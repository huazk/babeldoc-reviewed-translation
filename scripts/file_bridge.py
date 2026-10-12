"""BabelDOC 0.6.4 adapter: collect requests, validate reviewed files, render.

No external translation provider is used. Public model/font downloads are a
separate concern; BabelDOC may fetch missing assets. See references/implementation.md.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading


def configure_assets(path):
    # Windows spawn imports this module before unpickling BabelDOC workers.
    # Set its cache root there too, before translator.cache opens SQLite.
    import babeldoc.const as const
    assets = Path(path).resolve()
    assets.mkdir(parents=True, exist_ok=True)
    const.CACHE_FOLDER = assets
    const.TIKTOKEN_CACHE_FOLDER = assets / "tiktoken"
    const.TIKTOKEN_CACHE_FOLDER.mkdir(exist_ok=True)
    os.environ["TIKTOKEN_CACHE_DIR"] = str(const.TIKTOKEN_CACHE_FOLDER)


if os.environ.get("REVIEW_BABELDOC_ASSETS"):
    configure_assets(os.environ["REVIEW_BABELDOC_ASSETS"])


def unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"Duplicate JSON key: {key!r}")
        out[key] = value
    return out


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def check_mapping(sources, mapping, keep):
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise ValueError("source-index must be a list of strings")
    if not isinstance(mapping, dict) or not isinstance(keep, dict):
        raise ValueError("translations and keep must be JSON objects")
    errors = []
    for source in sources:
        target = mapping.get(source)
        if not isinstance(target, str) or not target.strip():
            errors.append({"source": source, "error": "missing or empty translation"})
            continue
        if Counter(re.findall(r"</?b\d+>", source)) != Counter(re.findall(r"</?b\d+>", target)):
            errors.append({"source": source, "error": "placeholder mismatch"})
        if source == target and (not isinstance(keep.get(source), str) or not keep[source].strip()):
            errors.append({"source": source, "error": "unchanged text needs a keep reason"})
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["assets", "collect", "check", "render"])
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--pages", help="BabelDOC page selector; identical for collect and render")
    parser.add_argument("--lang-in", default="en")
    parser.add_argument("--lang-out", default="zh")
    parser.add_argument("--python-path", action="append", default=[])
    parser.add_argument("--numpy-bytes-compat", action="store_true")
    args = parser.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=True)
    mapping = read_json(work / "translations.json") if (work / "translations.json").exists() else {}
    keep = read_json(work / "keep.json") if (work / "keep.json").exists() else {}
    if args.mode in ("check", "render"):
        errors = check_mapping(read_json(work / "source-index.json"), mapping, keep)
        write_json(work / "mapping-errors.json", errors)
        if errors:
            raise SystemExit(f"Mapping rejected: {len(errors)} errors; see mapping-errors.json")
        if args.mode == "check":
            print("MAPPING_OK", len(read_json(work / "source-index.json")))
            return
    for path in reversed(args.python_path):
        sys.path.insert(0, str(Path(path).resolve()))
    assets = (args.assets or work / "assets").resolve()
    os.environ["REVIEW_BABELDOC_ASSETS"] = str(assets)
    configure_assets(assets)
    if args.mode == "assets":
        import asyncio
        from babeldoc.assets.assets import get_doclayout_onnx_model_path_async, get_font_and_metadata_async, get_font_family
        async def prepare():
            names = set(sum(get_font_family(args.lang_out).values(), []))
            await asyncio.gather(get_doclayout_onnx_model_path_async(), *(get_font_and_metadata_async(f) for f in names))
        asyncio.run(prepare())
        print("ASSETS_READY", assets)
        return
    if not args.input or not args.input.is_file():
        parser.error("collect/render require an existing --input PDF")
    settings = {
        "sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
        "lang_in": args.lang_in, "lang_out": args.lang_out, "pages": args.pages,
        "disable_rich_text_translate": True, "split_short_lines": False,
        "min_text_length": 2, "skip_clean": True, "auto_extract_glossary": False,
        "disable_same_text_fallback": True, "numpy_bytes_compat": args.numpy_bytes_compat,
    }
    from importlib.metadata import version
    settings["babeldoc_version"] = version("babeldoc")
    if args.mode == "render" and read_json(work / "manifest.json") != settings:
        raise SystemExit("Input, version or configuration changed. Collect again in a new work directory.")
    if args.mode == "collect":
        if (work / "manifest.json").exists() and read_json(work / "manifest.json") != settings:
            raise SystemExit("Work directory belongs to a different input/configuration. Use a new directory.")
        write_json(work / "manifest.json", settings)
    if args.numpy_bytes_compat:
        import numpy as np
        original_fromstring = np.fromstring
        def compatible_fromstring(value, dtype=float, count=-1, *, sep="", **kwargs):
            if not sep and isinstance(value, bytes):
                return np.frombuffer(value, dtype=dtype, count=count, **kwargs)
            return original_fromstring(value, dtype=dtype, count=count, sep=sep, **kwargs)
        np.fromstring = compatible_fromstring
    from babeldoc.translator.translator import BaseTranslator, set_translate_rate_limiter
    from babeldoc.format.pdf.translation_config import TranslationConfig, WatermarkOutputMode
    from babeldoc.format.pdf.high_level import translate

    class FileTranslator(BaseTranslator):
        name = "reviewed-file"
        def __init__(self):
            super().__init__(args.lang_in, args.lang_out, True)
            self.model = "assistant-reviewed-file"
            self.items = {}
            self.lock = threading.Lock()

        def do_llm_translate(self, text, rate_limit_params=None, **kwargs):
            raise NotImplementedError("This adapter uses plain translation; rich mode is disabled.")

        def do_translate(self, text, rate_limit_params=None, **kwargs):
            value = mapping.get(text) if args.mode == "render" else None
            with self.lock:
                self.items[text] = value
            # Collection is identity-only. Unknown render items are recorded and
            # fail the run below, never silently accepted as a finished output.
            return value if value is not None else text

    translator = FileTranslator()
    set_translate_rate_limiter(1000)  # local dictionary lookup, no service calls
    config = TranslationConfig(
        translator=translator, input_file=str(args.input.resolve()),
        lang_in=args.lang_in, lang_out=args.lang_out, doc_layout_model=None,
        output_dir=str(work / ("collection" if args.mode == "collect" else "rendered")),
        working_dir=str(work / ("work-" + args.mode)), pages=args.pages,
        no_dual=True, watermark_output_mode=WatermarkOutputMode.NoWatermark,
        qps=8, use_rich_pbar=False, disable_rich_text_translate=True,
        split_short_lines=False, min_text_length=2, skip_clean=True,
        auto_extract_glossary=False, disable_same_text_fallback=True,
    )
    config._is_temp_dir = False
    succeeded = False
    try:
        result = translate(config)
        output = result.mono_pdf_path
        if output is None or not Path(output).is_file():
            raise RuntimeError("BabelDOC returned no readable output file")
        import fitz
        with fitz.open(output) as output_doc:
            if not len(output_doc):
                raise RuntimeError("BabelDOC returned an empty document")
            output_doc[0].get_pixmap()
        write_json(work / f"result-{args.mode}.json", {"mono_pdf": str(output)})
        succeeded = True
        print("RESULT", str(result))
    finally:
        write_json(work / f"requests-{args.mode}.json", translator.items)
        if args.mode == "collect" and succeeded:
            write_json(work / "source-index.json", sorted(translator.items))
        if args.mode == "render":
            missing = [s for s, v in translator.items.items() if v is None]
            write_json(work / "missing-render.json", missing)
    if args.mode == "render" and missing:
        raise SystemExit(f"Incomplete render: {len(missing)} new unmapped fragments. Update source-index and mappings; rerender.")
    print("REQUESTS", len(translator.items), "MODE", args.mode)


if __name__ == "__main__":
    main()
