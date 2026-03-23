#!/usr/bin/env python3
"""CDK application entry point for the SaaS Billing Agent infrastructure.

All resources deployed in a single CloudFormation stack.
Deployable via `cdk deploy`.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aws_cdk as cdk

from infra.stacks.unified_stack import UnifiedStack

app = cdk.App()

UnifiedStack(app, "SaaSBillingStack", env=cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT", os.environ.get("AWS_ACCOUNT_ID")),
    region="us-east-1",
))

app.synth()
