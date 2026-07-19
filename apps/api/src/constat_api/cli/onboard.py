"""Organization onboarding CLI (batch onboarding, roadmap 1.3).

Discovers the ACTIVE accounts of an AWS Organization and prints an
import-ready CSV to stdout:

    python -m constat_api.cli.onboard \
        --org-role-arn arn:aws:iam::111111111111:role/constat-org-reader \
        --external-id <mgmt-role-external-id> \
        > targets.csv
    curl -X POST .../collect/targets/import -H 'content-type: text/csv' \
        --data-binary @targets.csv

--org-role-arn / --external-id are for the MANAGEMENT-account role used
for discovery (needs organizations:ListAccounts only — see
collectors/organizations.py). They are NOT written to the CSV.

The CSV's external_id column is the PER-ACCOUNT collector secret (F-06:
each member account's constat-collector role needs its own ExternalId).
Two modes:
  - default: a REPLACE_ME placeholder the operator fills in per account;
  - --generate-external-ids: a fresh random value per account, to be
    deployed into each member account's trust policy (e.g. via the
    infra/customer/stackset.yaml StackSet) before the first collect.

Secret discipline: the CSV (placeholders or generated ids) goes to
stdout only; logs never carry external ids or ARNs.
"""

from __future__ import annotations

import argparse
import csv
import logging
import secrets
import sys

from botocore.exceptions import BotoCoreError, ClientError

from constat_api.collectors.organizations import list_org_accounts
from constat_api.settings import get_base_aws_session

logger = logging.getLogger(__name__)

# Default matches the role name the SaaS-side IAM policy restricts
# AssumeRole to ('arn:aws:iam::*:role/constat-collector*', infra/iam.tf).
DEFAULT_ROLE_TEMPLATE = "arn:aws:iam::{account_id}:role/constat-collector"

EXTERNAL_ID_PLACEHOLDER = "REPLACE_ME"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List AWS Organizations accounts as an import-ready collect-targets CSV.",
    )
    parser.add_argument(
        "--org-role-arn",
        required=True,
        help="Role ARN in the management account (needs organizations:ListAccounts only)",
    )
    parser.add_argument(
        "--external-id",
        required=True,
        help="ExternalId for assuming --org-role-arn (management role, not written to the CSV)",
    )
    parser.add_argument(
        "--role-template",
        default=DEFAULT_ROLE_TEMPLATE,
        help="Collector role ARN template; {account_id} is replaced per account "
        f"(default: {DEFAULT_ROLE_TEMPLATE})",
    )
    parser.add_argument(
        "--generate-external-ids",
        action="store_true",
        help="Generate a random per-account external_id instead of the REPLACE_ME placeholder",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose logging")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if "{account_id}" not in args.role_template:
        logger.error("--role-template must contain {account_id}")
        return 1

    base_session = get_base_aws_session()
    try:
        accounts = list_org_accounts(base_session, args.org_role_arn, args.external_id)
    except (ClientError, BotoCoreError) as e:
        # No ARNs / external ids in the log line beyond what boto puts in
        # the exception itself (AWS error messages quote the role ARN,
        # which is not a secret).
        logger.error("organizations discovery failed: %s", e)
        return 2

    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(["aws_account_id", "role_arn", "external_id", "name"])
    for account in accounts:
        external_id = (
            secrets.token_urlsafe(24) if args.generate_external_ids else EXTERNAL_ID_PLACEHOLDER
        )
        writer.writerow(
            [
                account["aws_account_id"],
                args.role_template.replace("{account_id}", account["aws_account_id"]),
                external_id,
                account["name"],
            ]
        )
    logger.info("wrote %d account rows to stdout", len(accounts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
