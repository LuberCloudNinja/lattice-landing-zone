#!/usr/bin/env python3
"""Curl the Lattice service with and without SigV4 signing -- SPEC.md Section
5, L4d. Demonstrates the auth policies in lattice_stack.py actually enforce
zero-trust: signed requests from inside app-vpc are allowed, unsigned
requests are denied.

The service's auth policies (ServiceNetworkAuthPolicy / ServiceAuthPolicy)
condition on aws:SourceVpc == app-vpc's VPC id. That means:
  - This MUST be run from inside app-vpc (e.g. an SSM session on
    ThreeTierStack's app-tier instance, or NetworkStack's AppTestHost) --
    running it from a laptop over the public internet will get denied even
    with a perfectly valid SigV4 signature, because the source VPC condition
    won't match at all.
  - The IAM principal signing the request needs `vpc-lattice-svcs:Invoke`
    allowed on it somewhere (e.g. the instance role) -- the auth policy's
    Principal is "*", so any authenticated (correctly signed) principal
    satisfying the SourceVpc condition is allowed; an EC2 instance's own
    role/instance profile credentials are enough.

Usage (from inside app-vpc, e.g. via SSM):
    python3 test-sigv4.py <service-dns-name-or-ip> [--port 80]

Requires: boto3 (already present in botocore-shipping AMIs / anywhere the
AWS CLI is installed) and `requests` (pip install requests if missing).
"""

import argparse
import sys

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def signed_get(url: str, region: str) -> requests.Response:
    session = boto3.Session()
    credentials = session.get_credentials()
    if credentials is None:
        print("No AWS credentials found -- run this from an instance with an IAM role attached.", file=sys.stderr)
        sys.exit(1)

    request = AWSRequest(method="GET", url=url)
    SigV4Auth(credentials, "vpc-lattice-svcs", region).add_auth(request)
    return requests.get(url, headers=dict(request.headers))


def unsigned_get(url: str) -> requests.Response:
    return requests.get(url)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host", help="Service DNS name or IP (see LatticeStack's ServiceDnsName output)")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--region", default="us-east-1")
    args = parser.parse_args()

    url = f"http://{args.host}:{args.port}/"

    print(f"--- WITHOUT SigV4 (expect 403 Forbidden -- auth policy denies unsigned/no-condition-match requests) ---")
    try:
        resp = unsigned_get(url)
        print(f"status={resp.status_code} body={resp.text[:200]}")
    except requests.RequestException as e:
        print(f"request failed: {e}")

    print()
    print(f"--- WITH SigV4 (expect 200 OK -- IF run from inside app-vpc; see module docstring) ---")
    try:
        resp = signed_get(url, args.region)
        print(f"status={resp.status_code} body={resp.text[:200]}")
    except requests.RequestException as e:
        print(f"request failed: {e}")


if __name__ == "__main__":
    main()
