"""Fuzz config loading: any TOML / JSON document is either a valid Config or a clear ConfigError."""
import json
import sys

import atheris

with atheris.instrument_imports():
    from dataclasses import fields

    from mssql_ocp_migrate.config import Config, ConfigError, ConnectionConfig, LiftConfig, MigrationConfig, ValidationConfig

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


SECTIONS = {"source": ConnectionConfig, "target": ConnectionConfig, "migration": MigrationConfig,
            "validation": ValidationConfig, "lift": LiftConfig}


def _scalar(fdp, depth=0):
    kind = fdp.ConsumeIntInRange(0, 7 if depth < 2 else 5)
    if kind == 0:
        return fdp.ConsumeIntInRange(-5, 100000)
    if kind == 1:
        return fdp.ConsumeBool()
    if kind == 2:
        return fdp.ConsumeRegularFloat()
    if kind == 3:
        return None
    if kind in (4, 5):
        return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 12))
    if kind == 6:
        return [_scalar(fdp, depth + 1) for _ in range(fdp.ConsumeIntInRange(0, 3))]
    return {fdp.ConsumeUnicodeNoSurrogates(4): _scalar(fdp, depth + 1) for _ in range(fdp.ConsumeIntInRange(0, 2))}


def _structured(fdp):
    doc = {}
    for section, dc in SECTIONS.items():
        if fdp.ConsumeIntInRange(0, 4) == 0:
            continue
        if fdp.ConsumeIntInRange(0, 9) == 0:
            doc[section] = _scalar(fdp)
            continue
        names = [f.name for f in fields(dc)]
        body = {}
        for _ in range(fdp.ConsumeIntInRange(0, 6)):
            key = names[fdp.ConsumeIntInRange(0, len(names) - 1)] if fdp.ConsumeIntInRange(0, 9) else "bogus"
            body[key] = _scalar(fdp)
        if section in ("source", "target") and fdp.ConsumeIntInRange(0, 3):
            body.setdefault("database", "db")
        doc[section] = body
    if fdp.ConsumeBool():
        doc["report_dir"] = _scalar(fdp)
    return doc


def _check(doc) -> None:
    try:
        cfg = Config.from_dict(doc)
    except ConfigError:
        return
    cfg.to_dict()
    cfg.resolved_executor()
    cfg.selects("dbo", "t")


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    if fdp.ConsumeBool():
        _check(_structured(fdp))
        return
    text = fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes())
    for parse in ([tomllib.loads] if tomllib else []) + [json.loads]:
        try:
            doc = parse(text)
        except (ValueError, TypeError, RecursionError):
            continue
        if isinstance(doc, dict):
            _check(doc)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
