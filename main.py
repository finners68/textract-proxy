import os
import boto3
import base64
import uuid
import logging
import time
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from botocore.exceptions import ClientError, BotoCoreError
from typing import Dict

# Setup
app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment variables
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_KEY")
AWS_REGION = os.getenv("AWS_REGION")
S3_BUCKET = os.getenv("S3_BUCKET")

if not all([AWS_ACCESS_KEY, AWS_SECRET_KEY, AWS_REGION, S3_BUCKET]):
    raise EnvironmentError("Missing one or more AWS environment variables.")

# AWS clients
s3 = boto3.client(
    "s3",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)

textract = boto3.client(
    "textract",
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)

# Input model
class ReceiptUpload(BaseModel):
    file: str  # base64-encoded PDF

@app.post("/process-receipt")
def process_receipt(data: ReceiptUpload):
    try:
        b64 = data.file.strip()
        if "," in b64:
            b64 = b64.split(",", 1)[1]  # Remove data URI prefix

        file_bytes = base64.b64decode(b64)

        if not file_bytes.startswith(b"%PDF"):
            return JSONResponse(status_code=400, content={"error": "Uploaded file is not a valid PDF."})

        filename = f"{uuid.uuid4()}.pdf"

        # Upload to S3
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=filename,
            Body=file_bytes,
            ContentType="application/pdf"
        )

        # Confirm S3 object exists
        for _ in range(10):
            try:
                s3.head_object(Bucket=S3_BUCKET, Key=filename)
                break
            except ClientError:
                time.sleep(1)
        else:
            return JSONResponse(status_code=504, content={"error": "S3 object not found after upload."})

        # Textract
        response = textract.analyze_document(
            Document={'S3Object': {'Bucket': S3_BUCKET, 'Name': filename}},
            FeatureTypes=["FORMS"]
        )

        fields = extract_fields(response['Blocks'])
        return {"fields": fields}

    except textract.exceptions.UnsupportedDocumentException:
        return JSONResponse(status_code=400, content={"error": "Unsupported document format."})
    except (ClientError, BotoCoreError) as e:
        logger.exception("AWS client error")
        return JSONResponse(status_code=502, content={"error": f"AWS error: {str(e)}"})
    except Exception as e:
        logger.exception("Unhandled error")
        return JSONResponse(status_code=500, content={"error": f"Internal error: {str(e)}"})

def extract_fields(blocks) -> Dict[str, str]:
    block_map = {b['Id']: b for b in blocks}
    key_map = {}
    value_map = {}

    for block in blocks:
        if block['BlockType'] == 'KEY_VALUE_SET':
            if 'KEY' in block.get('EntityTypes', []):
                key_map[block['Id']] = block
            elif 'VALUE' in block.get('EntityTypes', []):
                value_map[block['Id']] = block

    results = {}
    for key_id, key_block in key_map.items():
        key_text = get_text(key_block, block_map)
        if not key_text:
            continue

        value_texts = []
        for rel in key_block.get("Relationships", []):
            if rel["Type"] == "VALUE":
                for val_id in rel.get("Ids", []):
                    val_block = value_map.get(val_id)
                    val_text = get_text(val_block, block_map)
                    if val_text:
                        value_texts.append(val_text)

        if value_texts:
            results[key_text] = " | ".join(value_texts)

    return results

def get_text(block, block_map):
    if not block or "Relationships" not in block:
        return ""
    words = []
    for rel in block["Relationships"]:
        if rel["Type"] == "CHILD":
            for id in rel["Ids"]:
                word = block_map.get(id)
                if word and word.get("BlockType") == "WORD":
                    words.append(word.get("Text", ""))
    return " ".join(words)
