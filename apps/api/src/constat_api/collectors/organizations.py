"""AWS Organizations discovery (batch onboarding, roadmap 1.3).

Lists the ACTIVE member accounts of an AWS Organization so the onboarding
CLI can emit an import-ready CSV for POST /collect/targets/import —
35 accounts discovered in one API call, not 35 copy-pastes.

IAM: this assumes a role in the ORGANIZATION MANAGEMENT account (or a
delegated administrator). That role needs exactly one permission —
`organizations:ListAccounts`. It is NOT the per-account collector role
and needs no RDS/EC2 rights.

The AssumeRole reuses the AWS collector's `_assume_role` (mandatory
ExternalId, F-06) and the shared adaptive retry config: STS and
Organizations both throttle, and a failed AssumeRole here fails the
whole discovery.
"""

from __future__ import annotations

import logging

import boto3
from constat_core.collectors.aws import ADAPTIVE_RETRY_CONFIG

from constat_api.collectors.aws import TargetAccount, _assume_role

logger = logging.getLogger(__name__)


def list_org_accounts(
    base_session: boto3.Session,
    management_role_arn: str,
    external_id: str,
) -> list[dict[str, str]]:
    """List the ACTIVE accounts of the organization.

    Returns [{"aws_account_id", "name", "email"}], ordered as returned by
    the API (paginated organizations:ListAccounts). Suspended accounts
    are skipped — a suspended account would fail AssumeRole downstream,
    so onboarding it is noise.

    AssumeRole / ListAccounts failures propagate as ClientError: the CLI
    maps them to a clean exit code, and there is no partial-result state
    worth hiding behind a fallback.
    """
    mgmt_session = _assume_role(
        base_session,
        TargetAccount(
            # Label only — used in _assume_role's error messages; the real
            # account id comes from the assumed role, we don't know it yet.
            aws_account_id="organizations-management",
            role_arn=management_role_arn,
            external_id=external_id,
        ),
    )
    org = mgmt_session.client("organizations", config=ADAPTIVE_RETRY_CONFIG)
    paginator = org.get_paginator("list_accounts")

    accounts: list[dict[str, str]] = []
    for page in paginator.paginate():
        for account in page["Accounts"]:
            if account.get("Status") != "ACTIVE":
                logger.info(
                    "skipping non-ACTIVE account %s (status=%s)",
                    account["Id"],
                    account.get("Status"),
                )
                continue
            accounts.append(
                {
                    "aws_account_id": account["Id"],
                    "name": account.get("Name", ""),
                    "email": account.get("Email", ""),
                }
            )
    return accounts
