from __future__ import annotations

from typing import Any

from library_db import LibraryDB


def register_library_tools(mcp: Any) -> None:
    """Register source-neutral private-library tools on the Tokisclone MCP server."""

    @mcp.tool()
    def library_status() -> dict[str, Any]:
        """Return private catalogue status without touching source websites."""
        db = LibraryDB()
        dramas = db.list_dramas(limit=500)
        return {
            "ok": True,
            "database": str(db.path),
            "dramas": len(dramas),
            "episode_rows": sum(int(item.get("stored_episode_rows") or 0) for item in dramas),
            "verified_files": sum(int(item.get("verified_files") or 0) for item in dramas),
            "storage_model": "provider-neutral",
        }

    @mcp.tool()
    def library_list_dramas(limit: int = 100) -> dict[str, Any]:
        """List dramas from Tokisclone's own catalogue, newest first."""
        items = LibraryDB().list_dramas(limit=limit)
        return {"count": len(items), "dramas": items}

    @mcp.tool()
    def library_get_drama(slug_or_id: str) -> dict[str, Any]:
        """Get one drama with its sources, episodes, and independently stored files."""
        item = LibraryDB().get_drama(slug_or_id)
        return {"found": item is not None, "drama": item}
