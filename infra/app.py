#!/usr/bin/env python3
"""CDK application entry point for the SaaS Billing Agent infrastructure.

All resources deployed in a single CloudFormation stack.
Deployable via `cdk deploy`.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks, NagSuppressions

from infra.stacks.unified_stack import UnifiedStack

app = cdk.App()

stack = UnifiedStack(app, "SaaSBillingStack", env=cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT", os.environ.get("AWS_ACCOUNT_ID")),
    region="us-east-1",
))

# Add AWS Solutions security/best-practice checks
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))

# Suppress known acceptable findings for this demo/dev stack
NagSuppressions.add_stack_suppressions(stack, [
    {"id": "AwsSolutions-IAM4", "reason": "Using AWS managed policies is acceptable for AgentCore agent role"},
    {"id": "AwsSolutions-IAM5", "reason": "Wildcard permissions required for AgentCore, ECR, and CloudWatch operations"},
    {"id": "AwsSolutions-L1", "reason": "Python 3.12 is the latest supported Lambda runtime for AgentCore SDK"},
    {"id": "AwsSolutions-CB4", "reason": "CodeBuild encryption not required for demo container builds"},
    {"id": "AwsSolutions-S1", "reason": "S3 access logging not required for demo frontend bucket"},
    {"id": "AwsSolutions-CFR1", "reason": "CloudFront geo restrictions not required for demo"},
    {"id": "AwsSolutions-CFR2", "reason": "CloudFront WAF not required for demo"},
    {"id": "AwsSolutions-CFR4", "reason": "CloudFront custom SSL certificate not required for demo"},
    {"id": "AwsSolutions-COG1", "reason": "Cognito advanced password policy not required for demo"},
    {"id": "AwsSolutions-COG2", "reason": "Cognito MFA not required for demo"},
    {"id": "AwsSolutions-COG3", "reason": "Cognito AdvancedSecurityMode not required for demo"},
])

app.synth()
