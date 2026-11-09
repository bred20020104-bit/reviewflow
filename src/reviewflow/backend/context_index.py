"""Local bounded LF chunk/search service; not an implicit model retrieval tool."""

from reviewflow.review.context import ReviewContext
from reviewflow.review.contracts import EvidenceRef
from reviewflow.review.workflow_models import CodePacket


class ContextIndex:
    def __init__(self, source, inventory, *, max_bytes=131072, max_chunks=100):
        if type(max_bytes) is not int or not 1 <= max_bytes <= 8388608:
            raise ValueError("Invalid context budget")
        if type(max_chunks) is not int or not 1 <= max_chunks <= 1000:
            raise ValueError("Invalid chunk budget")
        self.context = ReviewContext(source, inventory)
        packets, omissions = [], []
        used = 0
        self._paths = []
        for record in inventory.snapshot.records:
            if record.status != "included":
                continue
            self._paths.append(record.path)
            text = self.context.read_file(record.path)
            if not text:
                omissions.append((record.path, "empty_file"))
                continue
            parts = text.split("\n")
            lines = [p + "\n" for p in parts[:-1]]
            if parts[-1]:
                lines.append(parts[-1])
            if any(len(line.encode("utf-8")) > 32768 for line in lines):
                omissions.append((record.path, "line_too_large"))
                continue
            start = 0
            while start < len(lines):
                stop, size = start, 0
                while stop < min(start + 200, len(lines)):
                    amount = len(lines[stop].encode("utf-8"))
                    if size + amount > 32768:
                        break
                    size += amount
                    stop += 1
                if used + size > max_bytes or len(packets) >= max_chunks:
                    omissions.append((record.path, "context_budget"))
                    break
                packets.append(
                    CodePacket(
                        EvidenceRef(
                            snapshot_id=inventory.snapshot.snapshot_id,
                            path=record.path,
                            content_sha256=record.sha256,
                            start_line=start + 1,
                            end_line=stop,
                        ),
                        "".join(lines[start:stop]),
                    )
                )
                used += size
                start = stop
        self.packets = tuple(packets)
        self.omissions = tuple(omissions)

    def search(self, literal: str, *, limit=10, allowed_paths=None):
        if (
            not isinstance(literal, str)
            or not literal.strip()
            or len(literal) > 200
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("Invalid literal query or limit")
        scope = set(self._paths) if allowed_paths is None else set(allowed_paths)
        if not scope <= set(self._paths):
            raise ValueError("Unknown search scope")
        # Check even paths whose old text would not match, so changes cannot hide silently.
        for path in self._paths:
            self.context.read_file(path)
        return tuple(p for p in self.packets if p.evidence.path in scope and literal in p.text)[
            :limit
        ]
