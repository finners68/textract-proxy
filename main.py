import boto3
import base64
import os
import uuid
import logging
import time
import botocore
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

app = FastAPI()

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Change this for production
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load and validate AWS credentials
AWS_ACCESS_KEY = os.environ.get("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.environ.get("AWS_SECRET_KEY")
AWS_REGION = os.environ.get("AWS_REGION")
S3_BUCKET = os.environ.get("S3_BUCKET")

if not all([AWS_ACCESS_KEY, AWS_SECRET_KEY, AWS_REGION, S3_BUCKET]):
    raise EnvironmentError("Missing one or more required AWS environment variables.")

# Boto3 clients
s3_client = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION,
)

textract_client = boto3.client(
    "textract",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION,
)

MAX_FILE_SIZE_MB = 5


def wait_for_s3_object(bucket, key, timeout=20):
    for _ in range(timeout):
        try:
            s3_client.head_object(Bucket=bucket, Key=key)
            return True
        except botocore.exceptions.ClientError:
            time.sleep(1)
    return False


@app.post("/process-receipt")
async def process_receipt(request: Request):
    try:
        body = await request.json()
        if 'file' not in body:
            raise ValueError("Missing 'file' in request body.")

        file_data_base64 = body['file']

        # Strip base64 header if present
        if "," in file_data_base64:
            file_data_base64 = file_data_base64.split(",")[1]

        # Basic file size check
        if len(file_data_base64) > MAX_FILE_SIZE_MB * 1024 * 1024 * 1.33:
            return JSONResponse(status_code=413, content={"error": "File too large."})

        # Decode base64
        try:
            file_bytes = base64.b64decode(file_data_base64)
        except Exception as e:
            logger.error(f"Failed to decode base64: {str(e)}")
            return JSONResponse(status_code=400, content={"error": "Base64 decoding failed."})

        # Validate it's a PDF by checking magic bytes
        if not file_bytes.startswith(b"%PDF"):
            return JSONResponse(status_code=400, content={"error": "Invalid PDF format."})

        filename = f"{uuid.uuid4()}.pdf"

        # Upload to S3
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=filename,
            Body=file_bytes,
            ContentType='application/pdf'
        )

        if not wait_for_s3_object(S3_BUCKET, filename):
            raise TimeoutError("S3 object not available after upload.")

        # Call Textract
        response = textract_client.analyze_document(
            Document={'S3Object': {'Bucket': S3_BUCKET, 'Name': filename}},
            FeatureTypes=["FORMS"]
        )

        return JSONResponse(status_code=200, content=response)

    except Exception as e:
        logger.exception("🔥 Error during processing")
        return JSONResponse(status_code=500, content={"error": str(e)})
