"""Tests for AWS Organizations discovery (roadmap 1.3 batch onboarding).

boto3 is stubbed: no network. The paginator is fed canned pages, and the
STS AssumeRole is intercepted via a mocked base session.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from constat_api.collectors.organizations import list_org_accounts

MGMT_ROLE_ARN = "arn:aws:iam::999999999999:role/constat-org-reader"
MGMT_EXTERNAL_ID = "mgmt-external-id"


def _base_session() -> tuple[MagicMock, MagicMock]:
    """A base boto3 session whose sts client returns canned credentials."""
    base = MagicMock()
    sts = MagicMock()
    sts.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "ASIATEST",
            "SecretAccessKey": "test-secret",
            "SessionToken": "test-token",
        }
    }
    base.client.return_value = sts
    return base, sts


def _mgmt_session(pages: list[dict]) -> MagicMock:
    """An assumed-role session whose organizations client paginates `pages`."""
    mgmt = MagicMock()
    org_client = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = pages
    org_client.get_paginator.return_value = paginator
    mgmt.client.return_value = org_client
    return mgmt


def test_list_org_accounts_parses_active_accounts_across_pages() -> None:
    base, sts = _base_session()
    mgmt = _mgmt_session(
        [
            {
                "Accounts": [
                    {"Id": "111111111111", "Name": "prod", "Email": "p@x.co", "Status": "ACTIVE"},
                    {"Id": "222222222222", "Name": "dev", "Email": "d@x.co", "Status": "ACTIVE"},
                ]
            },
            {
                "Accounts": [
                    {"Id": "333333333333", "Name": "old", "Email": "o@x.co", "Status": "SUSPENDED"},
                ]
            },
        ]
    )
    # _assume_role (collectors/aws.py) builds the assumed-role session via
    # boto3.Session(...) — patch it to hand back our stub.
    with patch("boto3.Session", return_value=mgmt):
        accounts = list_org_accounts(base, MGMT_ROLE_ARN, MGMT_EXTERNAL_ID)

    # SUSPENDED account skipped; ACTIVE ones parsed across both pages.
    assert accounts == [
        {"aws_account_id": "111111111111", "name": "prod", "email": "p@x.co"},
        {"aws_account_id": "222222222222", "name": "dev", "email": "d@x.co"},
    ]

    # F-06: the management AssumeRole carried the ExternalId.
    kwargs = sts.assume_role.call_args.kwargs
    assert kwargs["RoleArn"] == MGMT_ROLE_ARN
    assert kwargs["ExternalId"] == MGMT_EXTERNAL_ID

    # The organizations client/paginator used the right API.
    mgmt.client.assert_called_once()
    assert mgmt.client.call_args.args[0] == "organizations"
    mgmt.client.return_value.get_paginator.assert_called_once_with("list_accounts")


def test_list_org_accounts_assume_role_failure_surfaces() -> None:
    base, sts = _base_session()
    sts.assume_role.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "not authorized"}},
        "AssumeRole",
    )
    with pytest.raises(ClientError, match="AccessDenied"):
        list_org_accounts(base, MGMT_ROLE_ARN, MGMT_EXTERNAL_ID)


# ---------------------------------------------------------------------------
# CLI: python -m constat_api.cli.onboard
# ---------------------------------------------------------------------------


def test_onboard_cli_prints_import_ready_csv(capsys: pytest.CaptureFixture[str]) -> None:
    from constat_api.cli import onboard

    accounts = [
        {"aws_account_id": "111111111111", "name": "prod", "email": "p@x.co"},
        {"aws_account_id": "222222222222", "name": "dev", "email": "d@x.co"},
    ]
    with (
        patch("constat_api.cli.onboard.get_base_aws_session", return_value=MagicMock()),
        patch("constat_api.cli.onboard.list_org_accounts", return_value=accounts),
    ):
        exit_code = onboard.main(
            ["--org-role-arn", MGMT_ROLE_ARN, "--external-id", MGMT_EXTERNAL_ID]
        )

    assert exit_code == 0
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert lines[0] == "aws_account_id,role_arn,external_id,name"
    assert lines[1] == (
        "111111111111,arn:aws:iam::111111111111:role/constat-collector,REPLACE_ME,prod"
    )
    assert lines[2] == (
        "222222222222,arn:aws:iam::222222222222:role/constat-collector,REPLACE_ME,dev"
    )
    # The management-role external id is never printed.
    assert MGMT_EXTERNAL_ID not in out
