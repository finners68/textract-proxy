from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import boto3
import uuid
import base64
import os

app = FastAPI()

# Pull values from Render env vars
AWS_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY")
AWS_SECRET_KEY = os.getenv("AWS_SECRET_KEY")
AWS_REGION = os.getenv("AWS_REGION")
S3_BUCKET = os.getenv("S3_BUCKET")

s3 = boto3.client("s3", aws_access_key_id=AWS_ACCESS_KEY,
                        aws_secret_access_key=AWS_SECRET_KEY,
                        region_name=AWS_REGION)

textract = boto3.client("textract", aws_access_key_id=AWS_ACCESS_KEY,
                              aws_secret_access_key=AWS_SECRET_KEY,
                              region_name=AWS_REGION)

class ReceiptInput(BaseModel):
    image_base64: str

@app.post("/process-receipt")
def process_receipt(data: ReceiptInput):
    try:
        image_bytes = base64.b64decode(data.image_base64.split(",")[-1])
        filename = f"{uuid.uuid4()}.jpg"

        s3.put_object(Bucket=S3_BUCKET, Key=filename, Body=image_bytes)

        response = textract.analyze_expense(
            Document={'S3Object': {'Bucket': S3_BUCKET, 'Name': filename}}
        )

        summary = response['ExpenseDocuments'][0]['SummaryFields']
        def find_field(name):
            for field in summary:
                if field['Type']['Text'].upper() == name.upper():
                    return field.get('ValueDetection', {}).get('Text')
            return None

        return {
            "vendor": find_field("VENDOR_NAME"),
            "vat": find_field("TAX"),
            "total": find_field("TOTAL"),
            "date": find_field("INVOICE_RECEIPT_DATE")
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
