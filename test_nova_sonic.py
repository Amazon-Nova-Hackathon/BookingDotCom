"""Quick connectivity test for AWS Bedrock Nova Sonic.
Run: python test_nova_sonic.py
"""
import asyncio
import os
from dotenv import load_dotenv

load_dotenv()

async def test_connection():
    region = os.getenv("AWS_REGION", os.getenv("BEDROCK_REGION", "us-east-1"))
    model_id = os.getenv("BEDROCK_MODEL_ID", "amazon.nova-2-sonic-v1:0")
    access_key = os.getenv("AWS_ACCESS_KEY_ID", "")
    secret_key = os.getenv("AWS_SECRET_ACCESS_KEY", "")
    session_token = os.getenv("AWS_SESSION_TOKEN") or None

    print(f"Region  : {region}")
    print(f"Model   : {model_id}")
    print(f"Key ID  : {access_key[:8]}...{access_key[-4:] if len(access_key) > 12 else '(empty)'}")
    print()

    # ── Step 1: List Foundation Models via standard boto3 ──────────────────────
    print("Step 1: Listing Nova models via boto3 Bedrock client...")
    try:
        import boto3
        bedrock = boto3.client(
            "bedrock",
            region_name=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
        )
        models = bedrock.list_foundation_models(byProvider="Amazon")["modelSummaries"]
        nova_models = [m["modelId"] for m in models if "nova" in m["modelId"].lower()]
        print(f"  ✅ IAM credentials work! Found Nova models: {nova_models}")
    except Exception as e:
        print(f"  ❌ boto3 error: {e}")
        return

    # ── Step 2: Check model access with GetFoundationModel ─────────────────────
    print(f"\nStep 2: Checking model access for '{model_id}'...")
    try:
        response = bedrock.get_foundation_model(modelIdentifier=model_id)
        status = response.get("modelDetails", {}).get("modelLifecycle", {}).get("status", "?")
        print(f"  ✅ Model found! Status: {status}")
    except bedrock.exceptions.ResourceNotFoundException:
        print(f"  ❌ Model '{model_id}' NOT FOUND in region '{region}'. Try 'amazon.nova-sonic-v1:0'.")
        return
    except Exception as e:
        print(f"  ⚠️  Error: {e}")

    # ── Step 3: Check if model is actually accessible (invoke health check) ────
    print(f"\nStep 3: Testing model invoke access (with timeout=10s)...")
    try:
        from aws_sdk_bedrock_runtime.client import (
            BedrockRuntimeClient,
            InvokeModelWithBidirectionalStreamOperationInput,
        )
        from aws_sdk_bedrock_runtime.config import Config
        from smithy_aws_core.auth.sigv4 import SigV4AuthScheme
        from smithy_aws_core.identity.static import StaticCredentialsResolver

        config = Config(
            endpoint_uri=f"https://bedrock-runtime.{region}.amazonaws.com",
            region=region,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            aws_session_token=session_token,
            aws_credentials_identity_resolver=StaticCredentialsResolver(),
            auth_schemes={"aws.auth#sigv4": SigV4AuthScheme(service="bedrock")},
        )
        client = BedrockRuntimeClient(config=config)

        stream = await asyncio.wait_for(
            client.invoke_model_with_bidirectional_stream(
                InvokeModelWithBidirectionalStreamOperationInput(model_id=model_id)
            ),
            timeout=10.0,
        )
        print("  ✅ Bidirectional stream OPENED successfully! Nova Sonic is accessible.")
        await stream.close()

    except asyncio.TimeoutError:
        print("  ❌ TIMEOUT (10s) opening bidirectional stream.")
        print("  ➡️  Root cause: Model access is NOT approved in AWS Console.")
        print("  ➡️  Go to: AWS Console → Bedrock → Model Access → Enable 'Amazon Nova Sonic'")
    except Exception as e:
        print(f"  ❌ Error: {type(e).__name__}: {e}")
        if "AccessDenied" in str(e) or "UnauthorizedAccess" in str(e):
            print("  ➡️  Root cause: IAM user does NOT have Bedrock Nova Sonic access.")
        elif "ResourceNotFound" in str(e) or "ValidationException" in str(e):
            print(f"  ➡️  Root cause: Model '{model_id}' does not exist in region '{region}'.")

if __name__ == "__main__":
    asyncio.run(test_connection())
