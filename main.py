import os
import boto3
from fastapi import FastAPI, Request
from pydantic import BaseModel
from typing import Dict
from fastapi.responses import JSONResponse
from botocore.exceptions import ClientError, BotoCoreError

app = FastAPI()

# Load AWS credentials and config
aws_access_key = os.getenv("AWS_ACCESS_KEY")
aws_secret_key = os.getenv("AWS_SECRET_KEY")
aws_region = os.getenv("AWS_REGION")
s3_bucket = os.getenv("S3_BUCKET")

if not all([aws_access_key, aws_secret_key, aws_region, s3_bucket]):
    raise EnvironmentError("Missing one or more AWS environment variables.")

# AWS Clients
s3 = boto3.client(
    "s3",
    aws_access_key_id=aws_access_key,
    aws_secret_access_key=aws_secret_key,
    region_name=aws_region
)

textract = boto3.client(
    "textract",
    aws_access_key_id=aws_access_key,
    aws_secret_access_key=aws_secret_key,
    region_name=aws_region
)

# Request models
class UploadRequest(BaseModel):
    key: str
    contentType: str

@app.post("/get-upload-url")
def get_upload_url(data: UploadRequest):
    try:
        url = s3.generate_presigned_url(
            ClientMethod='put_object',
            Params={
                'Bucket': s3_bucket,
                'Key': data.key,
                'ContentType': data.contentType
            },
            ExpiresIn=300  # 5 minutes
        )
        return {"url": url}
    except ClientError as e:
        return JSONResponse(status_code=400, content={"error": f"S3 error: {str(e)}"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

class AnalyzeRequest(BaseModel):
    key: str

@app.post("/analyze")
def analyze(data: AnalyzeRequest):
    try:
        # Call AWS Textract
        response = textract.analyze_document(
            Document={
                'S3Object': {
                    'Bucket': s3_bucket,
                    'Name': data.key
                }
            },
            FeatureTypes=['FORMS']
        )

        # Parse and extract key-value data
        fields = extract_fields(response['Blocks'])
        return {"fields": fields}

    except textract.exceptions.UnsupportedDocumentException:
        return JSONResponse(status_code=400, content={"error": "Unsupported document format."})
    except ClientError as e:
        return JSONResponse(status_code=400, content={"error": f"Textract/S3 error: {str(e)}"})
    except BotoCoreError as e:
        return JSONResponse(status_code=500, content={"error": f"BotoCore error: {str(e)}"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": f"Unexpected error: {str(e)}"})

# Extract all key-value fields from Textract response
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

        value_texts = []
        for rel in key_block.get("Relationships", []):
            if rel["Type"] == "VALUE":
                for val_id in rel.get("Ids", []):
                    val_block = value_map.get(val_id)
                    val_text = get_text(val_block, block_map)
                    if val_text:
                        value_texts.append(val_text)

        results[key_text] = " | ".join(value_texts)

    return results

# Utility function to extract visible text
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
