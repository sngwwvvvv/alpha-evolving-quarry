from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol

from trading_desk.config import canonical_json, sha256_hex, utc_now
from trading_desk.ledger.bundle import LedgerBundle
from trading_desk.state.db import Database
from trading_desk.state.outbox import claim_outbox_due, mark_outbox_published
from trading_desk.storage.artifacts import ArtifactStore

WIKI_TOPIC = "wiki_publish"
NAMESPACES = frozenset({"backtest", "paper"})


class WikiPublishError(Exception):
    """Wiki sink rejected or failed a publication attempt."""


class WikiSink(Protocol):
    def publish(self, revision: PublicationRevision) -> str:
        """Store one revision. Must be idempotent and never overwrite."""


@dataclass(frozen=True, slots=True)
class PublicationRevision:
    revision_id: str
    namespace: str
    markdown: str
    json_payload: dict[str, Any]
    bundle_hash: str
    previous_revision_id: str | None = None
    run_id: str = ""
    version_id: str = ""
    kind: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "json_payload", dict(self.json_payload))

    def to_payload(self) -> dict[str, Any]:
        return {
            "bundle_hash": self.bundle_hash,
            "json_payload": self.json_payload,
            "kind": self.kind,
            "markdown": self.markdown,
            "namespace": self.namespace,
            "previous_revision_id": self.previous_revision_id,
            "revision_id": self.revision_id,
            "run_id": self.run_id,
            "version_id": self.version_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> PublicationRevision:
        body = payload["revision"] if "revision" in payload else payload
        previous = body.get("previous_revision_id")
        return cls(
            revision_id=str(body["revision_id"]),
            namespace=str(body["namespace"]),
            markdown=str(body["markdown"]),
            json_payload=dict(body["json_payload"]),
            bundle_hash=str(body["bundle_hash"]),
            previous_revision_id=None if previous is None else str(previous),
            run_id=str(body.get("run_id") or ""),
            version_id=str(body.get("version_id") or ""),
            kind=str(body.get("kind") or ""),
        )


class FakeWikiSink:
    """In-memory wiki. Tests only; no SQLite and no network."""

    def __init__(self) -> None:
        self.pages: dict[str, PublicationRevision] = {}
        self.order: list[str] = []

    def publish(self, revision: PublicationRevision) -> str:
        existing = self.pages.get(revision.revision_id)
        if existing is not None:
            if existing.bundle_hash != revision.bundle_hash:
                raise WikiPublishError("revision collision")
            return revision.revision_id
        self.pages[revision.revision_id] = revision
        self.order.append(revision.revision_id)
        return revision.revision_id

    def get(self, revision_id: str) -> PublicationRevision | None:
        return self.pages.get(revision_id)

    def overwrite(self, revision_id: str, markdown: str) -> None:
        raise WikiPublishError("overwrite forbidden")


def render_markdown(ledger: LedgerBundle, *, namespace: str, revision_id: str) -> str:
    failed = ", ".join(ledger.gates_failed) or "(none)"
    achieved = ", ".join(ledger.gates_achieved) or "(none)"
    refs = ", ".join(ledger.trade_references) or "(none)"
    return "\n".join(
        [
            f"# {namespace} ledger revision `{revision_id}`",
            "",
            f"- kind: {ledger.kind}",
            f"- outcome: {ledger.outcome}",
            f"- run_id: `{ledger.run_id}`",
            f"- version_id: `{ledger.version_id}`",
            f"- result_bundle_hash: `{ledger.result_bundle_hash}`",
            f"- ledger_bundle_hash: `{ledger.bundle_hash}`",
            "",
            "## Executive summary",
            "",
            ledger.executive_summary,
            "",
            "## Failed gates",
            "",
            failed,
            "",
            "## Achieved gates",
            "",
            achieved,
            "",
            "## Loss attribution",
            "",
            canonical_json(ledger.loss_attribution),
            "",
            "## Mutation hypothesis",
            "",
            ledger.mutation_hypothesis or "(none)",
            "",
            "## Prior version comparison",
            "",
            (
                canonical_json(ledger.prior_version_comparison)
                if ledger.prior_version_comparison
                else "(none)"
            ),
            "",
            "## Trade references",
            "",
            refs,
            "",
        ]
    )


def build_publication_revision(
    ledger: LedgerBundle,
    *,
    namespace: str,
    previous_revision_id: str | None = None,
) -> PublicationRevision:
    if namespace not in NAMESPACES:
        raise ValueError("namespace must be backtest or paper")
    revision_id = sha256_hex(
        canonical_json(
            {
                "bundle_hash": ledger.bundle_hash,
                "kind": ledger.kind,
                "namespace": namespace,
                "run_id": ledger.run_id,
            }
        )
    )
    return PublicationRevision(
        revision_id=revision_id,
        namespace=namespace,
        markdown=render_markdown(ledger, namespace=namespace, revision_id=revision_id),
        json_payload=ledger.to_payload(),
        bundle_hash=ledger.bundle_hash,
        previous_revision_id=previous_revision_id,
        run_id=ledger.run_id,
        version_id=ledger.version_id,
        kind=ledger.kind,
    )


def _idempotency_key(revision_id: str) -> str:
    return f"wiki:{revision_id}"


def process_publish_outbox(
    db: Database,
    sink: WikiSink,
    *,
    now: datetime | None = None,
) -> list[str]:
    published: list[str] = []
    for claim in claim_outbox_due(db, now=now, topic=WIKI_TOPIC):
        revision = PublicationRevision.from_payload(claim.payload)
        try:
            sink.publish(revision)
        except Exception:
            continue
        mark_outbox_published(
            db,
            outbox_id=claim.outbox_id,
            published_revision_id=revision.revision_id,
        )
        published.append(revision.revision_id)
    return published


def publish_revision(
    db: Database,
    store: ArtifactStore,
    ledger: LedgerBundle,
    *,
    sink: WikiSink,
    namespace: str = "backtest",
    now: datetime | None = None,
    previous_revision_id: str | None = None,
) -> PublicationRevision:
    now = now or utc_now()
    revision = build_publication_revision(
        ledger,
        namespace=namespace,
        previous_revision_id=previous_revision_id,
    )
    store.put_json(revision.to_payload())
    store.put_bytes(revision.markdown.encode("utf-8"))
    try:
        db.enqueue_outbox(
            topic=WIKI_TOPIC,
            payload={"revision": revision.to_payload()},
            idempotency_key=_idempotency_key(revision.revision_id),
            created_at=now.isoformat(),
        )
    except sqlite3.IntegrityError:
        pass
    process_publish_outbox(db, sink, now=now)
    return revision
