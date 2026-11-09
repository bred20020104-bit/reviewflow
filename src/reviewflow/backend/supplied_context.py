"""Validated unique-line packet budget shared by bounded dependency readers."""


class SuppliedContext:
    def __init__(self, context, limit, audit, overflow_error):
        self.context, self.limit, self.audit = context, limit, audit
        self.overflow_error = overflow_error
        self.packets, self.total = [], 0

    def supply(self, ref):
        # Even repeated reads must validate current bytes and ranges.
        self.context._validate_evidence(ref)
        covered = {line for p in self.packets
            if all(p["evidence"][key] == getattr(ref, key)
                   for key in ("path", "snapshot_id", "content_sha256"))
            for line in range(p["evidence"]["start_line"], p["evidence"]["end_line"] + 1)}
        additions, start = [], ref.start_line
        while start <= ref.end_line:
            if start in covered:
                start += 1
                continue
            end = start
            while end < ref.end_line and end + 1 not in covered:
                end += 1
            part = ref.model_copy(update={"start_line": start, "end_line": end})
            additions.append({"evidence": part.model_dump(mode="json"),
                              "text": self.context.read_lines(ref.path, start, end)})
            start = end + 1
        additional = sum(len(p["text"].encode("utf-8")) for p in additions)
        if self.total + additional > self.limit:
            self.audit["context_rejection"] = {"evidence": ref.model_dump(mode="json"),
                "buffered_bytes": self.total, "additional_bytes": additional, "limit_bytes": self.limit}
            raise self.overflow_error()
        self.total += additional
        self.packets.extend(additions)
