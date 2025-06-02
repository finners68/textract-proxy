from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import boto3
import base64
import os

app = FastAPI()

# Allow cross-origin requests (e.g., from Make.com)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load AWS credentials from environment
aws_access_key = os.getenv("AWS_ACCESS_KEY_ID")
aws_secret_key = os.getenv("AWS_SECRET_ACCESS_KEY")
aws_region = os.getenv("AWS_DEFAULT_REGION", "eu-central-1")

# Initialize Textract client
textract = boto3.client(
    "textract",
    aws_access_key_id=aws_access_key,
    aws_secret_access_key=aws_secret_key,
    region_name=aws_region,
)

@app.post("/process-receipt")
async def process_receipt(request: Request):
    try:
        body = await request.json()
        base64_file = body.get("file")

        if not base64_file:
            print("❌ Missing 'file' in request body.")
            raise HTTPException(status_code=400, detail="Missing 'file' in request body.")

        file_bytes = base64.b64decode(base64_file)
        print("📤 File received and decoded successfully.")

        response = textract.analyze_document(
            Document={"Bytes": file_bytes},
            FeatureTypes=["FORMS"]
        )

        print("✅ Textract analysis complete.")
        return JSONResponse(content=response, status_code=200)

    except Exception as e:
        print(f"🔥 Error during processing: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")
