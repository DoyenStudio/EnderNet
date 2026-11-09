# coding=utf-8
"""Fluid naming rules, with item-existence checks supplied by the caller."""


def _fluid_candidate(item_id, prefix="", suffix=""):
    # type: (str, str, str) -> str | None
    namespace, separator, name = item_id.partition(":")
    if not separator or not namespace or not name:
        return None
    if prefix:
        if not name.startswith(prefix):
            return None
        name = name[len(prefix):]
    if suffix:
        if not name.endswith(suffix):
            return None
        name = name[:-len(suffix)]
    return namespace + ":" + name if name else None


def build_fluid_catalog(registered_ids, item_exists):
    # type: (list[str], object) -> tuple[list[str], dict[str, str]]
    """Scan the unfiltered registry; buckets need no corresponding flowing item."""
    fluid_ids = set()
    bucket_fluid_ids = {}
    for item_id in sorted(set(registered_ids)):
        flowing = _fluid_candidate(item_id, prefix="flowing_")
        if flowing is not None and item_exists(flowing):
            fluid_ids.add(flowing)
        bucket = _fluid_candidate(item_id, suffix="_bucket")
        if bucket is not None and item_exists(bucket):
            bucket_fluid_ids[item_id] = bucket
    return sorted(fluid_ids), bucket_fluid_ids
