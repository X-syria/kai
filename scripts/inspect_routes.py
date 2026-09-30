"""Recursively dump every registered route (incl. FastAPI _IncludedRouter)."""

from kai.main import app

seen: set[tuple[str, object]] = set()


def walk(rs, depth: int = 0) -> None:
    pad = "  " * depth
    for r in rs:
        t = r.__class__.__name__
        path = getattr(r, "path", None)
        key = (t, path)
        if key in seen:
            continue
        seen.add(key)
        sub = getattr(r, "routes", None)
        if isinstance(sub, list) and sub:
            print(f"{pad}{t} {path}  (n={len(sub)})")
            walk(sub, depth + 1)
        else:
            methods = ",".join(sorted(getattr(r, "methods", []) or [])) or ""
            print(f"{pad}{t} {path}  [{methods}]")


walk(app.routes)
