import boto3
import base64
import os
import uuid
import logging
import time
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
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load AWS credentials from environment
AWS_ACCESS_KEY = os.environ.get("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.environ.get("AWS_SECRET_KEY")
AWS_REGION = os.environ.get("AWS_REGION")
S3_BUCKET = os.environ.get("S3_BUCKET")

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

@app.post("/process-receipt")
async def process_receipt(request: Request):
    try:
        body = await request.json()
        if 'file' not in body:
            raise ValueError("Missing 'file' in request body.")
        
        file_data_base64 = body['file']
        filename = f"{uuid.uuid4()}.pdf"

        # Decode base64
        try:
            file_bytes = base64.b64decode(file_data_base64)
        except Exception as e:
            logger.error(f"Failed to decode base64: {str(e)}")
            return JSONResponse(status_code=400, content={"error": "Base64 decoding failed."})

        # Upload to S3 with correct content type
        s3_client.put_object(
            Bucket=S3_BUCKET,
            Key=filename,
            Body=file_bytes,
            ContentType='application/pdf'
        )

        # Wait to ensure file is available
        time.sleep(10)
        s3_client.head_object(Bucket=S3_BUCKET, Key=filename)

        # Textract call
        response = textract_client.analyze_document(
            Document={'S3Object': {'Bucket': S3_BUCKET, 'Name': filename}},
            FeatureTypes=["FORMS"]
        )

        return JSONResponse(status_code=200, content=response)

    except Exception as e:
        logger.exception("🔥 Error during processing")
        return JSONResponse(status_code=500, content={"error": str(e)})
