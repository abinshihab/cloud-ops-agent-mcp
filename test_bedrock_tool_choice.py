import json

import boto3


bedrock = boto3.client(
    "bedrock-runtime",
    region_name="us-east-1",
)

tool_config = {
    "tools": [
        {
            "toolSpec": {
                "name": "get_recent_logs",
                "description": (
                    "Get recent application errors and warnings "
                    "for a cloud service."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "service": {
                                "type": "string",
                                "description": "Name of the service",
                            },
                            "lookback_minutes": {
                                "type": "integer",
                                "description": (
                                    "How many minutes of logs to retrieve"
                                ),
                            },
                        },
                        "required": ["service"],
                    }
                },
            }
        }
    ]
}

response = bedrock.converse(
    modelId="amazon.nova-lite-v1:0",
    system=[
        {
            "text": (
                "You are a cloud operations investigation agent. "
                "Use the available tools to collect evidence before "
                "reaching a conclusion."
            )
        }
    ],
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "text": (
                        "The checkout-api service has an HTTP 5xx rate "
                        "of 18.2%. Investigate the incident."
                    )
                }
            ],
        }
    ],
    toolConfig=tool_config,
    inferenceConfig={
        "maxTokens": 300,
        "temperature": 0,
    },
)

print("Stop reason:", response["stopReason"])

for content in response["output"]["message"]["content"]:
    if "text" in content:
        print("Model text:", content["text"])

    if "toolUse" in content:
        print("Tool requested:")
        print(json.dumps(content["toolUse"], indent=2))
