"""The machine-readable offline lead collector command line interface."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import errno
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import stat
import sys
from typing import TYPE_CHECKING, Callable, Literal, Mapping, NoReturn
from urllib.parse import urlparse
import uuid

from .ai import AiResult, AiSource, AssistInput
from .batch import BatchError, BatchOptions, enrich_csv
from .ego import capture_with_ego
from .exports import ExportError, ReviewRequest, csv_text, edit_row, exclude_row, json_text, parse_json_text, review_rows, write_export
from .extract import preview_document
from .jobs import JobRunner, RunnerDeps, control_job
from .model import FetchRequest, JobCheckpoint, JsonObject, JsonValue, JobSnapshot, LeadRow, ObservedValue, PageDocument, RecipeV1, Selection, json_array, json_object, observed_now, row_id
from .paths import is_protected_path
from .store import StoreConflict, StoreUnsafe, StoreWriteFailed, clear_jobs, load_job, managed_state_path, open_store, save_job
from .transport import TransportError, fetch_page, validate_fetch_request

if TYPE_CHECKING:
    from .assist import AssistCapture


MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_JOB_INPUT_BYTES = 128 * 1024
MAX_IMPORT_BYTES = 8 * 1024 * 1024
MAX_REPLAY_BYTES = 64 * 1024
DEFAULT_STATE = Path.home() / ".local" / "share" / "lead-collector" / "jobs.sqlite3"


class CliError(Exception):
    def __init__(self, code: str, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


class EnvelopeParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise CliError("INPUT_INVALID", "invalid command input", 2)


@dataclass(frozen=True)
class InputSpec:
    suffix: str
    label: str
    byte_limit: int = MAX_JOB_INPUT_BYTES


RECIPE_INPUT = InputSpec(".json", "recipe")
REPLAY_INPUT = InputSpec(".json", "replay", MAX_REPLAY_BYTES)
URL_INPUT = InputSpec(".txt", "URL")
EXPORT_INPUT = InputSpec(".json", "export", MAX_IMPORT_BYTES)


def _envelope(data: JsonObject | None, error: CliError | None = None) -> str:
    failure = None if error is None else {"code": error.code, "message": error.message}
    return json.dumps({"version": 1, "ok": error is None, "data": data, "error": failure}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _add_capture_source(command: argparse.ArgumentParser) -> None:
    source = command.add_mutually_exclusive_group(required=True)
    source.add_argument("--html", type=Path)
    source.add_argument("--url")


def _add_extraction_options(command: argparse.ArgumentParser) -> None:
    command.add_argument("--source-url")
    command.add_argument("--recipe", type=Path)
    command.add_argument("--mapping", default="{}")
    command.add_argument("--selection")


def _capture_command(commands: argparse._SubParsersAction[EnvelopeParser], name: str) -> EnvelopeParser:
    command = commands.add_parser(name)
    _add_capture_source(command)
    _add_extraction_options(command)
    return command


def _add_assist_command(commands: argparse._SubParsersAction[EnvelopeParser]) -> None:
    assist = _capture_command(commands, "assist")
    assist.add_argument("--ai", choices=("off", "scrapegraph", "jev", "hybrid"), default="off")
    assist.add_argument("--model")
    assist.add_argument("--allow-metered-jev", action="store_true")
    assist.add_argument("--request-preview", action="store_true")
    assist.add_argument("--replay", type=Path)


def _add_collect_command(commands: argparse._SubParsersAction[EnvelopeParser]) -> None:
    collect = _capture_command(commands, "collect")
    collect.add_argument("--render", choices=("ego",))
    collect.add_argument("--ego-task-space", type=int)
    collect.add_argument("--ego-page")


def _add_email_verify_command(commands: argparse._SubParsersAction[EnvelopeParser]) -> None:
    verifier = commands.add_parser("verify-email")
    verifier.add_argument("--email", required=True)
    verifier.add_argument("--reacher-binary", type=Path)


def _add_enrich_command(commands: argparse._SubParsersAction[EnvelopeParser]) -> None:
    enrich = commands.add_parser("enrich")
    enrich.add_argument("--input", required=True, type=Path)
    enrich.add_argument("--output", required=True, type=Path)
    enrich.add_argument("--verify-emails", action="store_true")
    enrich.add_argument("--max-sites", type=int, default=20)
    enrich.add_argument("--max-pages-per-site", type=int, default=3)
    enrich.add_argument("--max-emails", type=int, default=100)
    enrich.add_argument("--overwrite", action="store_true")
    enrich.add_argument("--reacher-binary", type=Path)


def _add_job_commands(commands: argparse._SubParsersAction[EnvelopeParser]) -> None:
    contacts = commands.add_parser("contacts")
    source = contacts.add_mutually_exclusive_group(required=True)
    source.add_argument("--urls", type=Path)
    source.add_argument("--job")
    contacts.add_argument("--row", action="append", default=[])
    contacts.add_argument("--recipe", required=True, type=Path)
    control = commands.add_parser("control")
    control.add_argument("--job", required=True)
    control.add_argument("--action", required=True, choices=("pause", "stop"))
    resume = commands.add_parser("resume")
    resume.add_argument("--job", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--job", required=True)


def _add_review_commands(commands: argparse._SubParsersAction[EnvelopeParser]) -> None:
    review = commands.add_parser("review")
    review.add_argument("--job", required=True)
    review.add_argument("--query", default="")
    review.add_argument("--sort", default="id", choices=("id", "company", "website", "address", "status"))
    review.add_argument("--row", action="append", default=[])
    edit = commands.add_parser("edit")
    edit.add_argument("--job", required=True)
    edit.add_argument("--row", required=True)
    edit.add_argument("--field", required=True, choices=("company", "website", "address"))
    edit.add_argument("--value", required=True)
    exclude = commands.add_parser("exclude")
    exclude.add_argument("--job", required=True)
    exclude.add_argument("--row", required=True)
    exported = commands.add_parser("export")
    exported.add_argument("--job", required=True)
    exported.add_argument("--format", required=True, choices=("csv", "json"))
    exported.add_argument("--output", required=True, type=Path)
    exported.add_argument("--row", required=True, action="append")
    exported.add_argument("--include-excluded", action="store_true")
    exported.add_argument("--overwrite", action="store_true")


def _add_local_commands(commands: argparse._SubParsersAction[EnvelopeParser]) -> None:
    imported = commands.add_parser("import")
    imported.add_argument("--input", required=True, type=Path)
    imported.add_argument("--job", required=True)
    clear = commands.add_parser("clear")
    clear.add_argument("--confirm-clear", action="store_true")
    recipe = commands.add_parser("recipe")
    recipe_commands = recipe.add_subparsers(dest="recipe_command", required=True)
    save_recipe = recipe_commands.add_parser("save")
    save_recipe.add_argument("--html", required=True, type=Path)
    save_recipe.add_argument("--source-url", required=True)
    save_recipe.add_argument("--mapping", default="{}")
    save_recipe.add_argument("--selection")
    save_recipe.add_argument("--output", required=True, type=Path)
    save_recipe.add_argument("--overwrite", action="store_true")
    preview_recipe = recipe_commands.add_parser("preview")
    preview_recipe.add_argument("--recipe", required=True, type=Path)
    preview_recipe.add_argument("--html", required=True, type=Path)
    preview_recipe.add_argument("--source-url", required=True)


def _parser() -> EnvelopeParser:
    parser = EnvelopeParser(prog="lead-collector")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, metavar="PATH", help="managed state leaf or direct path inside ~/.local/share/lead-collector")
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor")
    doctor.add_argument("--model")
    _add_assist_command(commands)
    _capture_command(commands, "preview")
    _add_collect_command(commands)
    _add_email_verify_command(commands)
    _add_enrich_command(commands)
    _add_job_commands(commands)
    _add_review_commands(commands)
    _add_local_commands(commands)
    return parser


def _unsafe_name(path: Path) -> bool:
    return any(_unsafe_component(component) for component in path.parts)


def _unsafe_component(component: str) -> bool:
    return is_protected_path(component)


def _open_html_descriptor(path: Path) -> int:
    if _unsafe_name(path):
        raise CliError("INPUT_UNSAFE", "unsafe HTML input path", 2)
    if path.suffix.lower() not in {".html", ".htm"}:
        raise CliError("INPUT_UNSAFE", "HTML input must use an HTML extension", 2)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if nofollow == 0 or directory == 0:
        raise CliError("CAPABILITY_UNAVAILABLE", "safe HTML input is unavailable", 3)
    absolute = path.absolute()
    descriptor: int | None = None
    try:
        descriptor = os.open(absolute.anchor, os.O_RDONLY | directory)
        for index, part in enumerate(absolute.parts[1:]):
            final = index == len(absolute.parts) - 2
            flags = os.O_RDONLY | nofollow
            if not final:
                flags |= directory
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise CliError("INPUT_UNSAFE", "unsafe HTML input path", 2) from exc
        raise CliError("INPUT_INVALID", "HTML input is unavailable", 2) from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise CliError("INPUT_UNSAFE", "unsafe HTML input path", 2)
    return descriptor


def _read_html(path: Path) -> str:
    descriptor = _open_html_descriptor(path)
    try:
        raw = bytearray()
        while len(raw) <= MAX_HTML_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, MAX_HTML_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
    finally:
        os.close(descriptor)
    if len(raw) > MAX_HTML_BYTES:
        raise CliError("HTML_TOO_LARGE", "HTML input exceeds the offline size limit", 2)
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CliError("RESPONSE_MALFORMED", "HTML input is not UTF-8", 2) from exc


def _read_job_input(path: Path, spec: InputSpec) -> str:
    if _unsafe_name(path) or path.suffix.lower() != spec.suffix:
        raise CliError("INPUT_UNSAFE", f"unsafe {spec.label} input path", 2)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if nofollow == 0 or directory == 0:
        raise CliError("CAPABILITY_UNAVAILABLE", f"safe {spec.label} input is unavailable", 3)
    absolute = path.absolute()
    descriptor: int | None = None
    try:
        descriptor = os.open(absolute.anchor, os.O_RDONLY | directory)
        for index, part in enumerate(absolute.parts[1:]):
            flags = os.O_RDONLY | nofollow
            if index != len(absolute.parts) - 2:
                flags |= directory
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
        raw = bytearray()
        while len(raw) <= spec.byte_limit:
            chunk = os.read(descriptor, min(64 * 1024, spec.byte_limit + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EINVAL}:
            raise CliError("INPUT_UNSAFE", f"unsafe {spec.label} input path", 2) from exc
        raise CliError("INPUT_INVALID", f"{spec.label} input is unavailable", 2) from exc
    os.close(descriptor)
    if len(raw) > spec.byte_limit:
        raise CliError("HTML_TOO_LARGE", f"{spec.label} input exceeds the size limit", 2)
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CliError("RESPONSE_MALFORMED", f"{spec.label} input is not UTF-8", 2) from exc


def _source_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise CliError("INPUT_INVALID", "source URL must be an explicit HTTP(S) provenance URL", 2)
    return value


def _object(value: str, label: str) -> Mapping[str, JsonValue]:
    try:
        parsed: object = json.loads(value)
        return json_object(parsed)
    except (json.JSONDecodeError, ValueError) as exc:
        raise CliError("INPUT_INVALID", f"{label} must be JSON object", 2) from exc


def _mapping(value: str) -> dict[str, str]:
    parsed = _object(value, "mapping")
    if not all(isinstance(key, str) and isinstance(item, str) and item in {"company", "website", "address"} for key, item in parsed.items()):
        raise CliError("INPUT_INVALID", "mapping contains an invalid field", 2)
    return {key: item for key, item in parsed.items() if isinstance(item, str)}


def _selection(value: str | None) -> Selection | None:
    if value is None:
        return None
    try:
        return Selection.from_json(_object(value, "selection"))
    except ValueError as exc:
        raise CliError("INPUT_INVALID", "selection is invalid", 2) from exc


def _recipe(source_url: str, selection: Selection, mapping: Mapping[str, str]) -> RecipeV1:
    parsed = urlparse(source_url)
    return RecipeV1(source_url, (parsed.hostname or "",), (parsed.path or "/",), selection, mapping, {"mode": "none", "selector": None}, {"maxInputs": 100, "maxPagesPerHost": 5, "maxRows": 1000, "maxPages": 100})


def _offline_document(source_url: str, html: str, observed_at: str) -> PageDocument:
    return PageDocument(source_url, html, observed_at, hashlib.sha256(html.encode("utf-8")).hexdigest(), "completed", None)


def _preview(args: argparse.Namespace) -> tuple[list[JsonObject], Selection, RecipeV1]:
    source_url = _source_url(args.source_url)
    recipe_path = getattr(args, "recipe", None)
    recipe = _recipe_file(recipe_path) if recipe_path is not None else None
    if recipe is not None:
        source_url = _validated_url(source_url, recipe)
    document = _offline_document(source_url, _read_html(args.html), observed_now())
    mapping = _mapping(args.mapping) if recipe is None else recipe.mapping
    expected_selection = _selection(args.selection) if recipe is None else recipe.selection
    try:
        rows, selection = preview_document(document, mapping, expected_selection)
    except ValueError as exc:
        if recipe is None:
            raise
        raise CliError("LAYOUT_DRIFT", "recipe selection requires review", 2) from exc
    active_recipe = _recipe(source_url, selection, mapping) if recipe is None else recipe
    return [row.to_json() for row in rows], selection, active_recipe


def _url_preview_recipe(url: str, args: argparse.Namespace) -> tuple[str, RecipeV1]:
    if args.recipe is not None:
        recipe = _recipe_file(args.recipe)
        return _validated_url(_source_url(url), recipe), recipe
    source_url = _source_url(url)
    selection = _selection(args.selection)
    parsed = urlparse(source_url)
    recipe = RecipeV1(source_url, (parsed.hostname or "",), (parsed.path or "/",), selection, _mapping(args.mapping), {"mode": "none", "selector": None}, {"maxInputs": 1, "maxPagesPerHost": 1, "maxRows": 1000, "maxPages": 1})
    return _validated_url(source_url, recipe), recipe


def _preview_url(args: argparse.Namespace) -> JsonObject:
    assert args.url is not None
    url, recipe = _url_preview_recipe(args.url, args)
    document = fetch_page(FetchRequest(url, recipe.allowed_hosts, recipe.allowed_paths, recipe.limits))
    if document.status not in {"completed", "empty"}:
        code = "JOB_FAILED" if document.error is None else str(document.error["code"])
        raise CliError(code, "URL preview failed", 1)
    rows, selection = preview_document(document, recipe.mapping, recipe.selection)
    return _preview_data([row.to_json() for row in rows], selection, replace(recipe, selection=selection))


def _preview_data(rows: list[JsonObject], selection: Selection, recipe: RecipeV1) -> JsonObject:
    return {
        "rows": json_array(rows),
        "selection": selection.to_json(),
        "recipe": recipe.to_json(),
    }


def _offline_snapshot(rows: list[JsonObject], recipe: RecipeV1, source_url: str) -> JobSnapshot:
    capped_rows = tuple(rows[: recipe.limits["maxRows"]])
    errors: tuple[JsonObject, ...] = ()
    if len(rows) >= recipe.limits["maxRows"]:
        errors = ({"code": "JOB_FAILED", "message": "row limit reached", "url": source_url, "status": "limited"},)
    return JobSnapshot(uuid.uuid4().hex, state="completed", progress={"attemptedPages": 1, "completedInputs": 1, "totalInputs": 1}, limits=dict(recipe.limits), rows=capped_rows, errors=errors, recipe=recipe)


def _verify_email_data(args: argparse.Namespace) -> JsonObject:
    from .email_verify import EmailVerifyRequest, verify_email

    result = verify_email(EmailVerifyRequest(args.email, args.reacher_binary))
    if result.error is not None:
        raise CliError(result.error.code, result.error.message, _verification_exit(result.error.code))
    return result.to_json()


def _verification_exit(code: str) -> int:
    if code == "CAPABILITY_UNAVAILABLE":
        return 3
    if code in {"INPUT_INVALID", "VERIFY_OUTPUT_INVALID"}:
        return 2
    return 1


def _state_path(path: Path) -> Path:
    try:
        return managed_state_path(path)
    except StoreUnsafe:
        raise CliError("INPUT_UNSAFE", "unsafe state path", 2)


def _recipe_file(path: Path) -> RecipeV1:
    try:
        return RecipeV1.from_json(_object(_read_job_input(path, RECIPE_INPUT), "recipe"))
    except ValueError as exc:
        raise CliError("INPUT_INVALID", "recipe is invalid", 2) from exc


def _assist_recipe(args: argparse.Namespace, source_url: str) -> tuple[str, Mapping[str, str], Selection | None]:
    if args.recipe is not None:
        recipe = _recipe_file(args.recipe)
        return _validated_url(source_url, recipe), recipe.mapping, recipe.selection
    return source_url, _mapping(args.mapping), _selection(args.selection)


def _assist_capture(args: argparse.Namespace) -> tuple[AssistInput, Mapping[str, str], Selection | None]:
    if args.html is not None:
        if args.source_url is None:
            raise CliError("INPUT_INVALID", "offline assist requires --source-url", 2)
        source_url, mapping, selection = _assist_recipe(args, _source_url(args.source_url))
        input_value = AssistInput(AiSource(source_url, observed_now(), "offline"), _read_html(args.html), args.ai, args.model)
        return input_value, mapping, selection
    if args.request_preview or args.replay is not None:
        raise CliError("INPUT_INVALID", "request preview and replay require owned HTML", 2)
    if args.url is None or args.recipe is None:
        raise CliError("INPUT_INVALID", "URL assist requires a recipe", 2)
    recipe = _recipe_file(args.recipe)
    url = _validated_url(_source_url(args.url), recipe)
    document = fetch_page(FetchRequest(url, recipe.allowed_hosts, recipe.allowed_paths, recipe.limits))
    if document.status not in {"completed", "empty"}:
        code = "JOB_FAILED" if document.error is None else str(document.error["code"])
        raise CliError(code, "URL assist failed", 3 if code == "CAPABILITY_UNAVAILABLE" else 1)
    input_value = AssistInput(AiSource(document.url, document.observed_at, "anonymous-http"), document.html, args.ai, args.model)
    return input_value, recipe.mapping, recipe.selection


def _validate_assist_options(args: argparse.Namespace) -> None:
    if args.ai in {"jev", "hybrid"} and not args.allow_metered_jev and not args.request_preview and args.replay is None:
        raise CliError("COST_POLICY", "metered Jev is disabled", 2)
    if args.request_preview and args.replay is not None:
        raise CliError("INPUT_INVALID", "choose request preview or replay", 2)


def _assist_contract(args: argparse.Namespace, capture: AssistCapture, data: JsonObject) -> JsonObject | None:
    if args.request_preview:
        from .jev import JevContractError, build_jev_request

        try:
            request = build_jev_request(capture.source, capture.candidates)
        except JevContractError as exc:
            raise CliError(exc.code, exc.message, 2) from exc
        return {**data, "request": request.payload, "contractOnly": True}
    if args.replay is None:
        return None
    from .jev import JevContractError, replay_jev_response

    try:
        result = replay_jev_response(capture.source, capture.candidates, _read_job_input(args.replay, REPLAY_INPUT).encode("utf-8"))
    except JevContractError as exc:
        raise CliError(exc.code, exc.message, 2) from exc
    return {**data, "ai": result.to_json(), "contractOnly": True}


def _disabled_assist(data: JsonObject) -> JsonObject:
    return {**data, "ai": {"status": "disabled", "accepted": [], "rejected": [], "model": None, "usage": None, "error": None, "contractOnly": False}}


def _active_assist(input_value: AssistInput, capture: AssistCapture, data: JsonObject) -> JsonObject:
    if input_value.mode == "scrapegraph":
        return {**data, "ai": _local_assist(input_value).to_json()}
    from .jev import JevContractError, build_jev_request, invoke_jev

    candidates = capture.candidates
    local_data: JsonObject | None = None
    if input_value.mode == "hybrid":
        from .assist import suggestion_candidates

        local = _local_assist(input_value)
        try:
            candidates = suggestion_candidates(local, capture.candidates)
        except ValueError as exc:
            raise CliError("AI_OUTPUT_INVALID", "local AI suggestions lack captured evidence", 2) from exc
        local_data = local.to_json()
    try:
        request = build_jev_request(capture.source, candidates)
    except JevContractError as exc:
        raise CliError(exc.code, exc.message, 2) from exc
    result = invoke_jev(request, allow_metered=True)
    _require_ai_result(result)
    response = {**data, "ai": result.to_json()}
    return response if local_data is None else {**response, "localAi": local_data}


def _assist_data(args: argparse.Namespace) -> JsonObject:
    _validate_assist_options(args)
    input_value, mapping, selection = _assist_capture(args)
    from .assist import captured_assist

    capture = captured_assist(input_value, mapping, selection)
    data = capture.to_json()
    contracted = _assist_contract(args, capture, data)
    if contracted is not None:
        return contracted
    return _disabled_assist(data) if input_value.mode == "off" else _active_assist(input_value, capture, data)


def _local_assist(input_value: AssistInput) -> AiResult:
    from .ai_runner import assist
    from .assist import reindex_suggestions

    local_input = AssistInput(input_value.source, input_value.html, "scrapegraph", input_value.model)
    result = reindex_suggestions(assist(local_input))
    _require_ai_result(result)
    return result


def _require_ai_result(result: AiResult) -> None:
    if result.error is not None:
        exit_code = 3 if result.error.code in {"SDK_UNAVAILABLE", "CAPABILITY_UNAVAILABLE"} else 2
        raise CliError(result.error.code, result.error.message, exit_code)
    if result.status != "completed":
        raise CliError("AI_OUTPUT_INVALID", "AI adapter returned an incomplete result", 2)


def _sdk_version() -> str | None:
    try:
        return importlib.metadata.version("scrapegraphai")
    except importlib.metadata.PackageNotFoundError:
        root = Path(__file__).resolve().parents[1] / ".venv-ai" / "lib"
        metadata_files = tuple(root.glob("python*/site-packages/scrapegraphai-*.dist-info/METADATA"))
        if len(metadata_files) != 1:
            return None
        try:
            for line in metadata_files[0].read_text(encoding="utf-8").splitlines():
                if line.startswith("Version: "):
                    return line.removeprefix("Version: ")
        except OSError:
            return None
    return None


def _urls_file(path: Path, recipe: RecipeV1) -> tuple[str, ...]:
    urls = tuple(_validated_url(_source_url(line.strip()), recipe) for line in _read_job_input(path, URL_INPUT).splitlines() if line.strip())
    if not urls:
        raise CliError("INPUT_INVALID", "URL input is empty", 2)
    if len(urls) > recipe.limits["maxInputs"]:
        raise CliError("INPUT_INVALID", "URL input exceeds the contact limit", 2)
    return urls


def _validated_url(url: str, recipe: RecipeV1) -> str:
    try:
        request = FetchRequest(url, recipe.allowed_hosts, recipe.allowed_paths, recipe.limits)
        return validate_fetch_request(request).url
    except (TransportError, ValueError):
        raise CliError("INPUT_INVALID", "URL is not approved by the recipe", 2) from None


def _runner_snapshot(job_id: str, recipe: RecipeV1, checkpoint: JobCheckpoint) -> JobSnapshot:
    return JobSnapshot(job_id, progress={"attemptedPages": 0, "completedInputs": 0, "totalInputs": len(checkpoint.input_urls)}, limits=dict(recipe.limits), recipe=recipe, checkpoint=checkpoint)


def _direct_contact_snapshot(urls: tuple[str, ...], recipe: RecipeV1) -> JobSnapshot:
    observed = observed_now()
    rows: list[JsonObject] = []
    associations: dict[str, tuple[str, ...]] = {}
    for index, url in enumerate(urls):
        website = ObservedValue(url, url, url, observed)
        owner = row_id((None, url, None), {"inputIndex": (str(index),)})
        rows.append(LeadRow(owner, website=website).to_json())
        associations[owner] = (url,)
    checkpoint = JobCheckpoint("contacts", urls, associations, tuple(dict.fromkeys(urls)), {}, (), ())
    return replace(_runner_snapshot(uuid.uuid4().hex, recipe, checkpoint), rows=tuple(rows))


def _source_contact_snapshot(state_path: Path, args: argparse.Namespace, recipe: RecipeV1) -> JobSnapshot:
    try:
        connection = open_store(state_path)
    except StoreUnsafe as exc:
        raise CliError("INPUT_UNSAFE", "state database is not a Lead Collector database", 2) from exc
    try:
        source = load_job(connection, args.job)
    finally:
        connection.close()
    if source is None:
        raise CliError("JOB_FAILED", "source job was not found", 1)
    requested = set(args.row)
    selected: list[LeadRow] = []
    for row_json in source.rows:
        try:
            row = LeadRow.from_json(row_json)
        except ValueError as exc:
            raise CliError("STORE_WRITE_FAILED", "source job rows are invalid", 1) from exc
        if requested and row.id not in requested:
            continue
        if row.excluded:
            if row.id in requested:
                raise CliError("INPUT_INVALID", "excluded rows cannot be contacted", 2)
            continue
        selected.append(row)
    if requested and {row.id for row in selected} != requested:
        raise CliError("INPUT_INVALID", "requested source row was not found", 2)
    if not selected:
        raise CliError("INPUT_INVALID", "source job has no eligible contact rows", 2)
    if len(selected) > recipe.limits["maxInputs"]:
        raise CliError("INPUT_INVALID", "source job exceeds the contact limit", 2)
    urls: list[str] = []
    associations: dict[str, tuple[str, ...]] = {}
    for row in selected:
        website = row.edits.get("website", "" if row.website is None else row.website.normalized)
        urls.append(_validated_url(_source_url(website), recipe))
        associations[row.id] = (urls[-1],)
    input_urls = tuple(urls)
    checkpoint = JobCheckpoint("contacts", input_urls, associations, tuple(dict.fromkeys(input_urls)), {}, (), ())
    return replace(_runner_snapshot(uuid.uuid4().hex, recipe, checkpoint), rows=tuple(row.to_json() for row in selected))


def _run_new_job(state_path: Path, snapshot: JobSnapshot) -> JsonObject:
    try:
        connection = open_store(state_path)
    except StoreUnsafe as exc:
        raise CliError("INPUT_UNSAFE", "state database is not a Lead Collector database", 2) from exc
    try:
        saved = save_job(connection, snapshot, 0)
        return JobRunner(connection, state_path, RunnerDeps(fetch_page, capture_with_ego)).run(saved.id).to_json()
    except StoreConflict as exc:
        raise CliError("STORE_CONFLICT", "checkpoint conflict", 1) from exc
    except StoreWriteFailed as exc:
        raise CliError("STORE_WRITE_FAILED", "checkpoint write failed", 1) from exc
    finally:
        connection.close()


def _run_existing_job(state_path: Path, job_id: str, action: Literal["pause", "stop"] | None) -> JsonObject:
    try:
        connection = open_store(state_path)
    except StoreUnsafe as exc:
        raise CliError("INPUT_UNSAFE", "state database is not a Lead Collector database", 2) from exc
    try:
        if action is None:
            return JobRunner(connection, state_path, RunnerDeps(fetch_page, capture_with_ego)).resume(job_id).to_json()
        return control_job(connection, job_id, action).to_json()
    except StoreConflict as exc:
        raise CliError("STORE_CONFLICT", "checkpoint conflict", 1) from exc
    except StoreWriteFailed as exc:
        raise CliError("STORE_WRITE_FAILED", "checkpoint write failed", 1) from exc
    finally:
        connection.close()


def _lead_rows(snapshot: JobSnapshot) -> tuple[LeadRow, ...]:
    try:
        return tuple(LeadRow.from_json(row) for row in snapshot.rows)
    except ValueError as exc:
        raise CliError("STORE_WRITE_FAILED", "saved rows are invalid", 1) from exc


def _review_job(state_path: Path, args: argparse.Namespace) -> JsonObject:
    try:
        connection = open_store(state_path)
    except StoreUnsafe as exc:
        raise CliError("INPUT_UNSAFE", "state database is not a Lead Collector database", 2) from exc
    try:
        snapshot = load_job(connection, args.job)
    finally:
        connection.close()
    if snapshot is None:
        raise CliError("JOB_FAILED", "job was not found", 1)
    try:
        rows = review_rows(_lead_rows(snapshot), ReviewRequest(args.query, args.sort, tuple(args.row)))
    except ExportError as exc:
        raise CliError("INPUT_INVALID", "review input is invalid", 2) from exc
    return {"jobId": snapshot.id, "rows": json_array(row.to_json() for row in rows)}


def _update_lead_row(state_path: Path, args: argparse.Namespace, update: Callable[[LeadRow], LeadRow]) -> JsonObject:
    try:
        connection = open_store(state_path)
    except StoreUnsafe as exc:
        raise CliError("INPUT_UNSAFE", "state database is not a Lead Collector database", 2) from exc
    try:
        snapshot = load_job(connection, args.job)
        if snapshot is None:
            raise CliError("JOB_FAILED", "job was not found", 1)
        changed: LeadRow | None = None
        rows: list[JsonObject] = []
        for row in _lead_rows(snapshot):
            current = update(row) if row.id == args.row else row
            if row.id == args.row:
                changed = current
            rows.append(current.to_json())
        if changed is None:
            raise CliError("INPUT_INVALID", "row was not found", 2)
        saved = save_job(connection, replace(snapshot, rows=tuple(rows)), snapshot.revision)
        return {"jobId": saved.id, "row": changed.to_json()}
    except StoreConflict as exc:
        raise CliError("STORE_CONFLICT", "checkpoint conflict", 1) from exc
    except StoreWriteFailed as exc:
        raise CliError("STORE_WRITE_FAILED", "checkpoint write failed", 1) from exc
    finally:
        connection.close()


def _export_job(state_path: Path, args: argparse.Namespace) -> JsonObject:
    try:
        connection = open_store(state_path)
    except StoreUnsafe as exc:
        raise CliError("INPUT_UNSAFE", "state database is not a Lead Collector database", 2) from exc
    try:
        snapshot = load_job(connection, args.job)
    finally:
        connection.close()
    if snapshot is None:
        raise CliError("JOB_FAILED", "job was not found", 1)
    rows = _lead_rows(snapshot)
    selected = set(args.row)
    if not selected or not selected.issubset({row.id for row in rows}):
        raise CliError("INPUT_INVALID", "selected export row was not found", 2)
    chosen = tuple(row for row in rows if row.id in selected and (args.include_excluded or not row.excluded))
    try:
        content = csv_text(chosen) if args.format == "csv" else json_text(chosen)
        write_export(args.output, content, overwrite=args.overwrite)
    except ExportError as exc:
        raise CliError(exc.code, "export output is invalid", 2 if exc.code != "CAPABILITY_UNAVAILABLE" else 3) from exc
    return {"jobId": snapshot.id, "format": args.format, "rows": len(chosen)}


def _import_job(state_path: Path, input_path: Path, job_id: str) -> JsonObject:
    try:
        rows = parse_json_text(_read_job_input(input_path, EXPORT_INPUT))
    except ExportError as exc:
        raise CliError(exc.code, "JSON export is invalid", 2 if exc.code != "CAPABILITY_UNAVAILABLE" else 3) from exc
    if not job_id:
        raise CliError("INPUT_INVALID", "local job ID is invalid", 2)
    snapshot = JobSnapshot(job_id, state="completed", progress={"attemptedPages": 0, "completedInputs": 0, "totalInputs": 0}, rows=tuple(row.to_json() for row in rows))
    try:
        connection = open_store(state_path)
    except StoreUnsafe as exc:
        raise CliError("INPUT_UNSAFE", "state database is not a Lead Collector database", 2) from exc
    try:
        return save_job(connection, snapshot, 0).to_json()
    except StoreConflict as exc:
        raise CliError("STORE_CONFLICT", "local job already exists", 1) from exc
    except StoreWriteFailed as exc:
        raise CliError("STORE_WRITE_FAILED", "checkpoint write failed", 1) from exc
    finally:
        connection.close()


def _save_recipe(args: argparse.Namespace) -> JsonObject:
    rows, selection, recipe = _preview(args)
    try:
        write_export(args.output, json.dumps(recipe.to_json(), ensure_ascii=False, sort_keys=True, separators=(",", ":")), overwrite=args.overwrite)
    except ExportError as exc:
        raise CliError(exc.code, "recipe output is invalid", 2 if exc.code != "CAPABILITY_UNAVAILABLE" else 3) from exc
    return _preview_data(rows, selection, recipe)


def _preview_recipe(args: argparse.Namespace) -> JsonObject:
    recipe = _recipe_file(args.recipe)
    source_url = _validated_url(_source_url(args.source_url), recipe)
    try:
        document = _offline_document(source_url, _read_html(args.html), observed_now())
        rows, selection = preview_document(document, recipe.mapping, recipe.selection)
    except ValueError as exc:
        raise CliError("LAYOUT_DRIFT", "recipe selection requires review", 2) from exc
    return _preview_data([row.to_json() for row in rows], selection, recipe)


def _clear_local_jobs(state_path: Path, confirmed: bool) -> JsonObject:
    if not confirmed:
        raise CliError("INPUT_INVALID", "clear requires explicit confirmation", 2)
    try:
        connection = open_store(state_path)
    except StoreUnsafe as exc:
        raise CliError("INPUT_UNSAFE", "state database is not a Lead Collector database", 2) from exc
    try:
        return {"cleared": clear_jobs(connection)}
    except StoreWriteFailed as exc:
        raise CliError("STORE_WRITE_FAILED", "checkpoint clear failed", 1) from exc
    finally:
        connection.close()


def _ego_renderer_config(args: argparse.Namespace) -> JsonObject | None:
    if args.render is None:
        if args.ego_task_space is not None or args.ego_page is not None:
            raise CliError("INPUT_INVALID", "Ego page options require --render ego", 2)
        return None
    if args.render != "ego" or args.ego_task_space is None or args.ego_task_space <= 0 or args.ego_page is None or not args.ego_page.strip():
        raise CliError("INPUT_INVALID", "--render ego requires a task space and page label", 2)
    return {"kind": "ego", "taskSpaceId": args.ego_task_space, "pageLabel": args.ego_page}


def _doctor(args: argparse.Namespace) -> JsonObject:
    import bs4
    import soupsieve

    capability: JsonObject = {"status": "unprobed"}
    if args.model is not None:
        from .ai_runner import _is_local_model, _probe_local_model

        if not _is_local_model(args.model):
            raise CliError("INPUT_INVALID", "an explicit local Ollama model is required", 2)
        unavailable = _probe_local_model(args.model)
        if unavailable is not None:
            raise CliError(unavailable.code, unavailable.message, 3)
        capability = {"status": "available", "model": args.model}
    return {"python": sys.version.split()[0], "beautifulsoup": bs4.__version__, "soupsieve": soupsieve.__version__, "network": "disabled", "ai": {"scrapegraphaiVersion": _sdk_version(), "explicitLocalModelRequired": True, "modelCapability": capability, "jevMetered": "disabled"}}


def _snapshot_job(state_path: Path, job_id: str) -> JsonObject:
    try:
        connection = open_store(state_path)
    except StoreUnsafe as exc:
        raise CliError("INPUT_UNSAFE", "state database is not a Lead Collector database", 2) from exc
    try:
        snapshot = load_job(connection, job_id)
    finally:
        connection.close()
    if snapshot is None:
        raise CliError("JOB_FAILED", "job was not found", 1)
    return snapshot.to_json()


def _state_command(state_path: Path, args: argparse.Namespace) -> JsonObject | None:
    if args.command == "snapshot":
        return _snapshot_job(state_path, args.job)
    if args.command == "control":
        return _run_existing_job(state_path, args.job, args.action)
    if args.command == "resume":
        return _run_existing_job(state_path, args.job, None)
    if args.command == "export":
        return _export_job(state_path, args)
    if args.command == "import":
        return _import_job(state_path, args.input, args.job)
    if args.command == "clear":
        return _clear_local_jobs(state_path, args.confirm_clear)
    if args.command == "recipe":
        return _save_recipe(args) if args.recipe_command == "save" else _preview_recipe(args)
    if args.command == "review":
        return _review_job(state_path, args)
    if args.command == "edit":
        return _update_lead_row(state_path, args, lambda row: edit_row(row, args.field, args.value))
    if args.command == "exclude":
        return _update_lead_row(state_path, args, exclude_row)
    return None


def _contacts_command(state_path: Path, args: argparse.Namespace) -> JsonObject:
    recipe = _recipe_file(args.recipe)
    if args.urls is not None:
        snapshot = _direct_contact_snapshot(_urls_file(args.urls, recipe), recipe)
    else:
        snapshot = _source_contact_snapshot(state_path, args, recipe)
    return _run_new_job(state_path, snapshot)


def _url_collect_command(state_path: Path, args: argparse.Namespace) -> JsonObject:
    if args.recipe is None:
        raise CliError("INPUT_INVALID", "URL collection requires a recipe", 2)
    recipe = _recipe_file(args.recipe)
    url = _validated_url(_source_url(args.url), recipe)
    checkpoint = JobCheckpoint("collect", (url,), {"input:0": (url,)}, (url,), {}, (), (), _ego_renderer_config(args))
    return _run_new_job(state_path, _runner_snapshot(uuid.uuid4().hex, recipe, checkpoint))


def _save_snapshot(state_path: Path, snapshot: JobSnapshot) -> JsonObject:
    try:
        connection = open_store(state_path)
    except StoreUnsafe as exc:
        raise CliError("INPUT_UNSAFE", "state database is not a Lead Collector database", 2) from exc
    try:
        saved = save_job(connection, snapshot, 0)
    except StoreConflict as exc:
        raise CliError("STORE_CONFLICT", "checkpoint conflict", 1) from exc
    except StoreWriteFailed as exc:
        raise CliError("STORE_WRITE_FAILED", "checkpoint write failed", 1) from exc
    finally:
        connection.close()
    return saved.to_json()


def _offline_command(state_path: Path, args: argparse.Namespace) -> JsonObject:
    if args.command == "collect" and (args.render is not None or args.ego_task_space is not None or args.ego_page is not None):
        raise CliError("INPUT_INVALID", "browser rendering requires URL collection", 2)
    rows, selection, recipe = _preview(args)
    if args.command == "preview":
        return _preview_data(rows, selection, recipe)
    return _save_snapshot(state_path, _offline_snapshot(rows, recipe, _source_url(args.source_url)))


def _run(args: argparse.Namespace) -> JsonObject:
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "assist":
        return _assist_data(args)
    if args.command == "verify-email":
        return _verify_email_data(args)
    if args.command == "enrich":
        try:
            return enrich_csv(args.input, args.output, BatchOptions(args.max_sites, args.max_pages_per_site, args.max_emails, args.verify_emails, args.overwrite, args.reacher_binary))
        except BatchError as exc:
            raise CliError(exc.code, exc.message, 3 if exc.code == "CAPABILITY_UNAVAILABLE" else 2) from exc
    state_path = _state_path(args.state)
    if args.command == "preview" and args.url is not None:
        return _preview_url(args)
    stored = _state_command(state_path, args)
    if stored is not None:
        return stored
    if args.command == "contacts":
        return _contacts_command(state_path, args)
    if args.command == "collect" and args.url is not None:
        return _url_collect_command(state_path, args)
    return _offline_command(state_path, args)


def _job_failure(command: str, data: JsonObject) -> tuple[str, int] | None:
    if command not in {"collect", "contacts", "resume"}:
        return None
    errors = data.get("errors")
    if isinstance(errors, list):
        for error in reversed(errors):
            if not isinstance(error, dict) or error.get("status") in {"limited", "empty"}:
                continue
            possible_code = error.get("code")
            if isinstance(possible_code, str):
                return possible_code, 3 if possible_code == "CAPABILITY_UNAVAILABLE" else 1
    if data.get("state") == "failed":
        return "JOB_FAILED", 1
    return None


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        data = _run(args)
        failure = _job_failure(args.command, data)
        if failure is not None:
            code, exit_code = failure
            print(_envelope(data, CliError(code, "job execution failed", exit_code)))
            return exit_code
        print(_envelope(data))
        return 0
    except CliError as exc:
        print(_envelope(None, exc))
        return exc.exit_code
    except StoreUnsafe:
        print(_envelope(None, CliError("INPUT_UNSAFE", "state database is not a Lead Collector database", 2)))
        return 2
    except Exception:
        print(_envelope(None, CliError("JOB_FAILED", "offline collection failed", 1)))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
