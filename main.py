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
async def process_receipt(request: Request):
    try:
        body = await request.json()
        image_data = body["image_base64"].split(",")[-1]
        img_bytes = base64.b64decode(image_data)
        file_name = f"receipts/{uuid.uuid4()}.pdf"

        s3.put_object(Bucket=BUCKET_NAME, Key=file_name, Body=img_bytes)

        response = textract.analyze_expense(Document={"S3Object": {"Bucket": BUCKET_NAME, "Name": file_name}})
        
        summary = response["ExpenseDocuments"][0]["SummaryFields"]
        output = {}
        for field in summary:
            key = field.get("Type", {}).get("Text", "").lower()
            val = field.get("ValueDetection", {}).get("Text", "")
            if key and val:
                output[key] = val
        return output

    except Exception as e:
        print("ERROR:", str(e))  # 👈 this will now show in Render’s logs
        raise HTTPException(status_code=500, detail="Processing failed.")


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
