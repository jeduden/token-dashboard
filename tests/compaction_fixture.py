"""Synthetic transcripts with compact_boundary + compact-summary records (made-up data only).

Pricing and tiers come from expiry_fixture: $1 per cache-read token, $10 per 5m write token.
"""
import json

from tests.expiry_fixture import Sess, iso

SECRET_CONTENT = "BOUNDARY-CONTENT-SECRET"
SECRET_SUMMARY = "SUMMARY-TEXT-SECRET private plan details"
SECRET_TOOL = "PrivateToolNameXyz"


class CSess(Sess):
    """Sess that can mark every record as belonging to a sub-agent thread and emit compactions."""

    def __init__(self, sid="s1", cwd=None, agent=None, session=None):
        """``sid`` prefixes uuids / message ids / the file name; ``session`` is the recorded
        sessionId (defaults to ``sid``), so a sub-agent file can share its parent's session."""
        super().__init__(sid, cwd)
        self.agent, self.session = agent, session or sid

    def _rec(self, sec, typ, message):
        super()._rec(sec, typ, message)
        r = json.loads(self.lines[-1])
        r["sessionId"] = self.session
        if self.agent:
            r["isSidechain"], r["agentId"] = True, self.agent
        self.lines[-1] = json.dumps(r)

    def boundary(self, sec, trigger="auto", pre=300000, post=15000, dur=30000, cum="auto",
                 metadata=True, agent=None, version="2.1.0", **extra):
        self.n += 1
        agent = agent or self.agent
        r = {"type": "system", "subtype": "compact_boundary", "uuid": "%s-b%d" % (self.sid, self.n),
             "parentUuid": None, "logicalParentUuid": self.last, "timestamp": iso(sec),
             "sessionId": self.session, "isSidechain": bool(agent), "cwd": self.cwd or "/work/x",
             "gitBranch": "main", "entrypoint": "cli", "version": version,
             "content": SECRET_CONTENT}
        if agent:
            r["agentId"] = agent
        if metadata:
            md = {"trigger": trigger, "preTokens": pre, "postTokens": post, "durationMs": dur,
                  "preservedSegment": {"headUuid": "h"}, "preCompactDiscoveredTools": [SECRET_TOOL]}
            md["cumulativeDroppedTokens"] = (None if post is None or pre is None else pre - post) if cum == "auto" else cum
            r["compactMetadata"] = {k: v for k, v in md.items() if v is not None}
        r.update(extra)
        self.lines.append(json.dumps(r))
        return r

    def summary(self, sec, text=SECRET_SUMMARY):
        self._rec(sec, "user", {"role": "user", "content": text})
        r = json.loads(self.lines[-1])
        r["isCompactSummary"] = True
        self.lines[-1] = json.dumps(r)
