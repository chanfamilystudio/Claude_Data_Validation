"""Period sign-off (spec section 8): preconditions, frozen evidence workbook, revoke."""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

from recon.config.loader import Project
from recon.excel.export import export_workbook
from recon.model import SignoffRow
from recon.repository import Repository


class SignoffRefused(Exception):
    """A sign-off or revoke was refused; ``reasons`` lists every failed precondition."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _is_signer(project: Project, repo: Repository, domain: str) -> tuple[bool, str]:
    user = repo.current_user()
    allowed = {s.casefold() for s in project.signers.get(domain, [])}
    return user.casefold() in allowed, user


def _signer_reason(user: str, domain: str) -> str:
    return f"{user} is not an authorized signer for {domain}"


def _evidence_path(project: Project, domain: str, period_label: str, now: datetime) -> Path:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return (project.settings.evidence_dir / domain / period_label
            / f"recon_evidence_{domain}_{period_label}_{stamp}.xlsx")


def sign_off(project: Project, repo: Repository, *, domain: str, period_label: str, comment: str,
             now: datetime) -> SignoffRow:
    """Approve ``domain`` + ``period_label``: check preconditions, freeze evidence, record the approval."""
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    reasons: list[str] = []

    run = repo.latest_run(domain, period_label)
    if run is None:
        reasons.append(f"no run found for {domain} {period_label}")
    else:
        if run.status != "succeeded":
            reasons.append(f"latest run {run.run_id} status is {run.status}")
        executed = {c.rule_id for c in repo.checks_for_run(run.run_id, domain)}
        required = {r.id for r in project.rules.values() if r.domain == domain and r.active}
        missing = sorted(required - executed)
        if missing:
            reasons.append(f"latest run {run.run_id} did not execute rules: {', '.join(missing)}")

    active = repo.breaks_for(domain, period_label, statuses={"open", "investigating"})
    if active:
        reasons.append(f"{len(active)} breaks are open or investigating")

    is_signer, user = _is_signer(project, repo, domain)
    if not is_signer:
        reasons.append(_signer_reason(user, domain))

    if repo.active_signoff(domain, period_label) is not None:
        reasons.append(f"{domain} {period_label} is already signed off")

    if reasons:
        raise SignoffRefused(reasons)
    assert run is not None

    accepted = len(repo.breaks_for(domain, period_label, statuses={"accepted"}))
    final = _evidence_path(project, domain, period_label, now).resolve()
    final.parent.mkdir(parents=True, exist_ok=True)
    # Export to a private temp file, then hard-link it into place: the link fails if the final name exists,
    # so a concurrent or earlier sign-off's frozen evidence is never overwritten (or cleaned up by us).
    tmp: Path | None = final.with_name(f".{final.name}.{uuid.uuid4().hex}.tmp")
    linked = False
    try:
        export_workbook(repo, domain=domain, period_label=period_label, run_id=run.run_id,
                        out_path=tmp, evidence=True)
        try:
            os.link(tmp, final)
        except FileExistsError:
            raise SignoffRefused([f"evidence file already exists: {final}; retry"]) from None
        linked = True
        tmp.unlink()
        tmp = None
        os.chmod(final, stat.S_IREAD)
        digest = hashlib.sha256(final.read_bytes()).hexdigest()
        # state may have changed while the workbook was being written
        latest = repo.latest_run(domain, period_label)
        still_active = repo.breaks_for(domain, period_label, statuses={"open", "investigating"})
        if latest is None or latest.run_id != run.run_id or still_active:
            raise SignoffRefused(["state changed during sign-off; retry"])
        repo.insert_signoff(SignoffRow(
            signoff_id=None, domain=domain, period_label=period_label, run_id=run.run_id, signed_by=user,
            signed_at=now, comment=comment, open_break_count=0, accepted_break_count=accepted,
            evidence_path=str(final), evidence_sha256=digest, status="signed",
        ))
    except BaseException:
        # delete only files this call created
        if tmp is not None and tmp.exists():
            tmp.unlink()
        if linked and final.exists():
            os.chmod(final, stat.S_IWRITE | stat.S_IREAD)
            final.unlink()
        raise
    stamped = repo.active_signoff(domain, period_label)
    assert stamped is not None
    return stamped


def revoke(project: Project, repo: Repository, *, domain: str, period_label: str, reason: str) -> None:
    """Revoke the active sign-off of ``domain`` + ``period_label``; the evidence file is kept."""
    if not reason or not reason.strip():
        raise SignoffRefused(["a reason is required"])
    reasons: list[str] = []
    active = repo.active_signoff(domain, period_label)
    if active is None:
        reasons.append(f"no active sign-off for {domain} {period_label}")
    is_signer, user = _is_signer(project, repo, domain)
    if not is_signer:
        reasons.append(_signer_reason(user, domain))
    if reasons:
        raise SignoffRefused(reasons)
    assert active is not None and active.signoff_id is not None
    repo.revoke_signoff(active.signoff_id, reason.strip())
