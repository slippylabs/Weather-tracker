"""Collector failures must leave a trace in the server log, not only in the
payload's per-source ok:false (which nobody on the server ever sees)."""
import logging, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import collect as c  # noqa: E402


def test_failed_source_is_logged(monkeypatch, caplog):
    def boom(_loc):
        raise RuntimeError("upstream said no")
    srcs = [{"id": "boom", "name": "Boom", "short": "B", "url": "u", "note": "", "fetch": boom}]
    monkeypatch.setattr(c, "build_sources", lambda loc: srcs)
    monkeypatch.setattr(c, "summarise", lambda *a, **k: {})
    with caplog.at_level(logging.WARNING, logger="weather.collect"):
        out = c.collect(c.Loc("X", 1.0, 2.0, "UTC"), days=["2026-01-01"])
    assert out["sources"][0]["ok"] is False
    assert any("boom" in r.getMessage() and "upstream said no" in r.getMessage()
               for r in caplog.records)
