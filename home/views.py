import hashlib
import json
import logging
import math
import re
import time
from urllib.parse import urlsplit

import requests
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_http_methods

logger = logging.getLogger(__name__)

API_BASE = "https://www.themealdb.com/api/json/v1/1/"


def text(value, limit=200):
    return value.strip()[:limit] if isinstance(value, str) else ""


def safe_url(value, hosts, prefix="/"):
    if not isinstance(value, str) or len(value) > 2000:
        return ""

    try:
        url = urlsplit(value)

        if (
            url.scheme == "https"
            and url.hostname in hosts
            and url.port in (None, 443)
            and not url.username
            and not url.password
            and url.path.startswith(prefix)
            and not any(ord(character) < 33 for character in value)
        ):
            return value

    except ValueError:
        pass

    return ""


def instruction_steps(instructions):
    """Keep the wording; split existing paragraphs or line breaks."""
    cleaned = instructions.replace("\r\n", "\n").replace("\r", "\n")

    cleaned = re.sub(
        r"(?im)^[ \t]*step[ \t]+\d+[ \t]*[:.)-]?[ \t]*$",
        "\n\n",
        cleaned,
    )

    paragraphs = [
        part.strip()
        for part in re.split(r"\n[ \t]*\n+", cleaned)
        if part.strip()
    ]

    if len(paragraphs) == 1:
        paragraphs = [
            line.strip()
            for line in paragraphs[0].splitlines()
            if line.strip()
        ]

    return paragraphs


def normalize_meal(payload):
    if not isinstance(payload, dict):
        raise ValueError("Invalid response")

    rows = payload.get("meals")

    if (
        not isinstance(rows, list)
        or len(rows) != 1
        or not isinstance(rows[0], dict)
    ):
        raise ValueError("Recipe not found")

    row = rows[0]
    meal_id = row.get("idMeal")
    name = text(row.get("strMeal"))

    if (
        not isinstance(meal_id, str)
        or not re.fullmatch(r"[0-9]{1,10}", meal_id)
        or not name
    ):
        raise ValueError("Invalid recipe identity")

    raw_instructions = row.get("strInstructions")

    if (
        raw_instructions is not None
        and not isinstance(raw_instructions, str)
    ):
        raise ValueError("Invalid instructions")

    instructions = (raw_instructions or "").strip()

    if len(instructions) > 100000:
        raise ValueError("Instructions too large")

    ingredients = []

    for index in range(1, 21):
        ingredient_name = text(
            row.get(f"strIngredient{index}"),
            300,
        )

        if ingredient_name:
            ingredients.append(
                {
                    "name": ingredient_name,
                    "measure": text(
                        row.get(f"strMeasure{index}"),
                        150,
                    ),
                }
            )

    return {
        "id": meal_id,
        "name": name,
        "category": text(row.get("strCategory")),
        "area": (
            text(row.get("strArea"))
            or text(row.get("strCountry"))
        ),
        "image": safe_url(
            row.get("strMealThumb"),
            {"www.themealdb.com", "themealdb.com"},
            "/images/media/meals/",
        ),
        "ingredients": ingredients,
        "instructions": instructions,
        "steps": instruction_steps(instructions),
    }


def fetch_meal(meal_id=""):
    endpoint = (
        f"lookup.php?i={meal_id}"
        if meal_id
        else "random.php"
    )

    started = time.monotonic()

    # Connection/read inactivity limits, not a total wall-clock deadline.
    with requests.get(
        API_BASE + endpoint,
        timeout=(3.05, 8),
        stream=True,
        allow_redirects=False,
        headers={"Accept": "application/json"},
    ) as response:
        response.raise_for_status()

        if response.status_code != 200:
            raise ValueError("Unexpected API status")

        body = bytearray()

        for chunk in response.iter_content(16384):
            body.extend(chunk)

            if (
                len(body) > 1048576
                or time.monotonic() - started > 15
            ):
                raise ValueError("API response exceeded budget")

        meal = normalize_meal(json.loads(body))

        if meal_id and meal["id"] != meal_id:
            raise ValueError("Unexpected recipe ID")

        return meal


@require_http_methods(["GET", "POST", "HEAD"])
@csrf_protect
def recipe(request):
    meal_id = request.GET.get(
        "meal",
        request.session.get("rh_current", ""),
    )

    if (
        request.method == "GET"
        and request.GET.get("new") == "1"
    ):
        meal_id = ""

    if meal_id and not re.fullmatch(r"[0-9]{1,10}", meal_id):
        return render(
            request,
            "recipe.html",
            {
                "error": (
                    "That recipe link is invalid. "
                    "Please choose another recipe."
                )
            },
            status=400,
        )

    key = f"night-kitchen:meal:v1:{meal_id}"
    meal = cache.get(key) if meal_id else None
    stale = False

    if meal is None:
        try:
            meal = fetch_meal(meal_id)

            actual_key = (
                f"night-kitchen:meal:v1:{meal['id']}"
            )

            cache.set(actual_key, meal, 3600)
            cache.set(actual_key + ":stale", meal, 86400)

        except (requests.RequestException, ValueError):
            logger.warning(
                "MealDB recipe unavailable",
                exc_info=True,
            )

            meal = (
                cache.get(key + ":stale")
                if meal_id
                else None
            )

            if meal is None:
                response = render(
                    request,
                    "recipe.html",
                    {
                        "error": (
                            "We couldn't load this recipe. "
                            "Please try again shortly."
                        )
                    },
                    status=503,
                )

                response["Retry-After"] = "60"
                return response

            stale = True

    meal_id = meal["id"]
    root = reverse("recipe")

    view = (
        "cook"
        if request.GET.get("view") == "cook"
        else "discover"
    )

    focus = (
        view == "cook"
        and request.GET.get("focus") == "1"
    )

    discover_url = f"{root}?meal={meal_id}"
    cook_url = discover_url + "&view=cook"

    page_url = (
        cook_url + ("&focus=1" if focus else "")
        if view == "cook"
        else discover_url
    )

    signature = hashlib.sha256(
        json.dumps(
            [meal["instructions"], meal["ingredients"]]
        ).encode()
    ).hexdigest()

    states = request.session.get("rh_progress", {})
    state = states.get(meal_id)

    if not state or state.get("signature") != signature:
        state = {
            "signature": signature,
            "index": 0,
            "completed": 0,
            "finished": False,
            "checked": [],
            "remaining": 600,
            "end": None,
            "timer_finished": False,
        }

    total = len(meal["steps"])
    now = time.time()

    if state["end"] is not None and state["end"] <= now:
        state.update(
            end=None,
            remaining=0,
            timer_finished=True,
        )

    saved = request.session.get("rh_saved", [])
    notice = request.session.pop("rh_notice", "")
    anchor = ""

    if request.method == "POST":
        action = request.POST.get("action", "")

        if action == "ingredients":
            selected = request.POST.getlist("ingredient")

            state["checked"] = [
                index
                for index in range(len(meal["ingredients"]))
                if str(index) in selected
            ]

            notice = "Ingredient checklist saved."
            anchor = "#ingredients"

        elif action == "next" and total:
            if state["finished"]:
                state.update(
                    index=0,
                    completed=0,
                    finished=False,
                )
            else:
                state["completed"] = max(
                    state["completed"],
                    state["index"] + 1,
                )

                if state["index"] < total - 1:
                    state["index"] += 1
                else:
                    state["finished"] = True

            anchor = "#workbench"

        elif action == "previous" and total:
            state["index"] = max(0, state["index"] - 1)
            state["finished"] = False
            anchor = "#workbench"

        elif action == "restart":
            state.update(
                index=0,
                completed=0,
                finished=False,
            )

            anchor = "#workbench"

        elif action == "timer_set":
            try:
                minutes = int(
                    request.POST.get("minutes", "")
                )

                if not 1 <= minutes <= 180:
                    raise ValueError

                state.update(
                    end=None,
                    remaining=minutes * 60,
                    timer_finished=False,
                )

                notice = "Timer set. Press Start when ready."

            except (ValueError, TypeError):
                notice = (
                    "Enter a whole number from "
                    "1 to 180 minutes."
                )

            anchor = "#timer"

        elif action == "timer_toggle":
            if state["end"] is not None:
                state["remaining"] = max(
                    0,
                    math.ceil(state["end"] - now),
                )

                state["end"] = None

            elif state["remaining"] > 0:
                state.update(
                    end=now + state["remaining"],
                    timer_finished=False,
                )

            anchor = "#timer"

        elif action == "bookmark":
            if any(item["id"] == meal_id for item in saved):
                saved = [
                    item
                    for item in saved
                    if item["id"] != meal_id
                ]

                notice = "Recipe removed from your cookbook."

            elif len(saved) < 50:
                saved.append(
                    {
                        "id": meal_id,
                        "name": meal["name"],
                    }
                )

                notice = "Recipe saved to your cookbook."

            else:
                notice = (
                    "Your cookbook has 50 recipes. "
                    "Remove one to save another."
                )

        elif action == "remove_saved":
            saved = [
                item
                for item in saved
                if item["id"] != request.POST.get("saved_id")
            ]

            anchor = "#saved-bookmarks"

        else:
            notice = "That action is unavailable."

    # Keep progress for the 20 most recently opened recipes.
    states.pop(meal_id, None)
    states[meal_id] = state

    request.session["rh_progress"] = dict(
        list(states.items())[-20:]
    )

    request.session["rh_saved"] = saved
    request.session["rh_current"] = meal_id

    # Redirect after submitting to avoid repeating an action on refresh.
    if request.method == "POST":
        request.session["rh_notice"] = notice
        return redirect(page_url + anchor)

    remaining = (
        max(0, math.ceil(state["end"] - now))
        if state["end"] is not None
        else state["remaining"]
    )

    ingredients = [
        dict(
            item,
            index=index,
            checked=index in state["checked"],
        )
        for index, item in enumerate(meal["ingredients"])
    ]

    response = render(
        request,
        "recipe.html",
        {
            "meal": meal,
            "stale": stale,
            "view": view,
            "focus": focus,
            "state": state,
            "ingredients": ingredients,
            "ready": len(state["checked"]),
            "total_steps": total,
            "step_number": state["index"] + 1,
            "current_step": (
                meal["steps"][state["index"]]
                if total
                else ""
            ),
            "last_step": (
                total > 0
                and state["index"] == total - 1
            ),
            "saved": saved,
            "is_saved": any(
                item["id"] == meal_id
                for item in saved
            ),
            "notice": notice,
            "page_url": page_url,
            "discover_url": discover_url,
            "cook_url": cook_url,
            "new_url": root + "?new=1",
            "focus_url": (
                cook_url
                if focus
                else cook_url + "&focus=1"
            ),
            "remaining": remaining,
            "timer_text": (
                f"{remaining // 60:02d}:"
                f"{remaining % 60:02d}"
            ),
            "timer_minutes": max(
                1,
                math.ceil(remaining / 60),
            ),
            "timer_end_ms": (
                int(state["end"] * 1000)
                if state["end"] is not None
                else ""
            ),
        },
    )

    response["Cache-Control"] = "no-store"
    return response