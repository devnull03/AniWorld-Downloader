"""English catalog, episode selection, and queue integration."""

from flask import jsonify, request

from ... import english_source
from .. import db, worker
from .api_queue import _current_username


def register(bp):
    bp.add_url_rule("/english/catalog", view_func=english_catalog)
    bp.add_url_rule("/english/details", view_func=english_details)
    bp.add_url_rule("/english/download", view_func=english_download, methods=["POST"])
    bp.add_url_rule("/english/check", view_func=check_english_source, methods=["POST"])


def english_catalog():
    keyword = request.args.get("q", "").strip()
    if len(keyword) > 200:
        return jsonify({"error": "Search must be 200 characters or fewer."}), 400
    try:
        results = english_source.catalog(keyword)
        return jsonify({"results": results, "download_supported": None})
    except english_source.SourceError as exc:
        return jsonify({"error": str(exc)}), 502


def english_details():
    try:
        season = int(request.args.get("season", 1))
        episode = int(request.args.get("episode", 1))
    except ValueError:
        return jsonify({"error": "Season and episode must be numbers."}), 400
    try:
        return jsonify(
            english_source.details(request.args.get("path", ""), season, episode)
        )
    except english_source.SourceError as exc:
        return jsonify({"error": str(exc)}), 502


def english_download():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object."}), 400
    path = data.get("path", "")
    episodes = data.get("episodes")
    season = data.get("season", 1)
    if (
        not isinstance(path, str)
        or not english_source.TITLE_PATH.fullmatch(path)
        or type(season) is not int
        or not 0 <= season <= 10000
        or not isinstance(episodes, list)
        or not 1 <= len(episodes) <= 500
        or any(
            type(number) is not int or not 1 <= number <= 10000 for number in episodes
        )
    ):
        return jsonify(
            {"error": "Select a valid title, season, and episode list."}
        ), 400
    custom_path_id = data.get("custom_path_id")
    if custom_path_id is not None and (
        type(custom_path_id) is not int or not db.get_custom_path(custom_path_id)
    ):
        return jsonify({"error": "The selected download folder no longer exists."}), 400
    try:
        document = english_source.details(path, season)
    except english_source.SourceError as exc:
        return jsonify({"error": str(exc)}), 502
    if not document["download_supported"]:
        return jsonify({"error": "This title has no supported download server."}), 422
    available = (
        {item["number"] for item in document["episodes"]}
        if document["type"] == "tv"
        else {1}
    )
    selected = sorted(set(episodes))
    if not set(selected).issubset(available):
        return jsonify(
            {"error": "An episode is not listed as available by the source."}
        ), 400
    entries = [
        {
            "english_path": path,
            "season": season,
            "episode": number,
            "url": f"{path}?s={season}&e={number}",
        }
        for number in selected
    ]
    queue_id = db.add_to_queue(
        title=document["title"],
        series_url=path,
        episodes=entries,
        language="Source Audio",
        provider="Vidrock",
        username=_current_username(),
        custom_path_id=custom_path_id,
    )
    worker.ensure_started()
    return jsonify({"queue_id": queue_id})


def check_english_source():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object."}), 400
    try:
        origin = english_source.normalize_base_url(data.get("base_url"))
    except english_source.SourceError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        return jsonify(english_source.check_address(origin))
    except english_source.SourceError as exc:
        return jsonify({"error": str(exc)}), 502
