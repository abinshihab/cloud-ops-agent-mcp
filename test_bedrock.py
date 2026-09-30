import boto3


bedrock = boto3.client(
    "bedrock-runtime",
    region_name="us-east-1",
)

response = bedrock.converse(
    modelId="amazon.nova-lite-v1:0",
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "text": (
                        "You are a cloud operations assistant. "
                        "Reply with exactly: Bedrock connection successful"
                    )
                }
            ],
        }
    ],
    inferenceConfig={
        "maxTokens": 100,
        "temperature": 0,
    },
)

message = response["output"]["message"]

for content in message["content"]:
    if "text" in content:
        print(content["text"])
