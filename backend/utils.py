import os
import json
import logging
import requests
import dataclasses

from urllib import parse
from azure.storage.blob import generate_blob_sas, BlobSasPermissions
from datetime import datetime, timedelta

STORAGE_ACCOUNT_NAME = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
STORAGE_ACCOUNT_KEY = os.environ.get("AZURE_STORAGE_ACCOUNT_KEY")
CONTAINER_NAME = os.environ.get("AZURE_STORAGE_CONTAINER_NAME")
DEBUG = os.environ.get("DEBUG", "false")

if DEBUG.lower() == "true":
    logging.basicConfig(level=logging.DEBUG)

AZURE_SEARCH_PERMITTED_GROUPS_COLUMN = os.environ.get("AZURE_SEARCH_PERMITTED_GROUPS_COLUMN")

class JSONEncoder(json.JSONEncoder):
    def default(self, o):
        if dataclasses.is_dataclass(o):
            return dataclasses.asdict(o)
        return super().default(o)

async def format_as_ndjson(r):
    try:
        async for event in r:
            yield json.dumps(event, cls=JSONEncoder) + "\n"
    except Exception as error:
        logging.exception("Exception while generating response stream: %s", error)
        yield json.dumps({"error": str(error)})

def parse_multi_columns(columns: str) -> list:
    if "|" in columns:
        return columns.split("|")
    else:
        return columns.split(",")


def fetchUserGroups(userToken, nextLink=None):
    # Recursively fetch group membership
    if nextLink:
        endpoint = nextLink
    else:
        endpoint = "https://graph.microsoft.com/v1.0/me/transitiveMemberOf?$select=id"
    
    headers = {
        'Authorization': "bearer " + userToken
    }
    try :
        r = requests.get(endpoint, headers=headers)
        if r.status_code != 200:
            logging.error(f"Error fetching user groups: {r.status_code} {r.text}")
            return []
        
        r = r.json()
        if "@odata.nextLink" in r:
            nextLinkData = fetchUserGroups(userToken, r["@odata.nextLink"])
            r['value'].extend(nextLinkData)
        
        return r['value']
    except Exception as e:
        logging.error(f"Exception in fetchUserGroups: {e}")
        return []


def generateFilterString(userToken):
    # Get list of groups user is a member of
    userGroups = fetchUserGroups(userToken)
    logging.info(f"USER GROUPS - {userGroups}")
    # Construct filter string
    if not userGroups:
        logging.debug("No user groups found")

    group_ids = ", ".join([obj['id'] for obj in userGroups])
    return f"{AZURE_SEARCH_PERMITTED_GROUPS_COLUMN}/any(g:search.in(g, '{group_ids}'))"

def format_non_streaming_response(chatCompletion, history_metadata, message_uuid=None):
    response_obj = {
        "id": chatCompletion.id,
        "model": chatCompletion.model,
        "created": chatCompletion.created,
        "object": chatCompletion.object,
        "choices": [
            {
                "messages": []
            }
        ],
        "history_metadata": history_metadata
    }

    if len(chatCompletion.choices) > 0:
        message = chatCompletion.choices[0].message
        if message:
            if hasattr(message, "context") and message.context.get("messages"):
                for m in message.context["messages"]:
                    if m["role"] == "tool":
                        response_obj["choices"][0]["messages"].append({
                            "role": "tool",
                            "content": m["content"]
                        })
            elif hasattr(message, "context"):
                response_obj["choices"][0]["messages"].append({
                    "role": "tool",
                    "content": json.dumps(message.context),
                })
            response_obj["choices"][0]["messages"].append({
                "role": "assistant",
                "content": message.content,
            })
            return response_obj
    
    return {}

def format_stream_response(chatCompletionChunk, history_metadata, message_uuid=None):
    response_obj = {
        "id": chatCompletionChunk.id,
        "model": chatCompletionChunk.model,
        "created": chatCompletionChunk.created,
        "object": chatCompletionChunk.object,
        "choices": [{
            "messages": []
        }],
        "history_metadata": history_metadata
    }

    if len(chatCompletionChunk.choices) > 0:
        delta = chatCompletionChunk.choices[0].delta
        if delta:
            if hasattr(delta, "context") and delta.context.get("messages"):
                for m in delta.context["messages"]:
                    if m["role"] == "tool":
                        # Generate SAS token which will be valid for 1 hour
                        m['content']=generate_sas_url(m["content"])
                        messageObj = {
                            "role": "tool",
                            "content": m["content"]
                        }
                        response_obj["choices"][0]["messages"].append(messageObj)
                        return response_obj
            if delta.role == "assistant" and hasattr(delta, "context"):
                messageObj = {
                    "role": "assistant",
                    "context": delta.context,
                }
                response_obj["choices"][0]["messages"].append(messageObj)
                return response_obj
            else:
                if delta.content:
                    messageObj = {
                        "role": "assistant",
                        "content": delta.content,
                    }
                    response_obj["choices"][0]["messages"].append(messageObj)
                    return response_obj
    
    return {}

def generate_sas_url(messageObj):
    messageObj=json.loads(messageObj)
    citations=messageObj['citations']

    for citation in citations:
        if citation['url']:
            blob_name=parse.unquote(citation['url'].split(f'core.windows.net/{CONTAINER_NAME}/')[1])
            print(f'BLOB NAME IS {blob_name}')
            sas_token = generate_blob_sas(account_name=STORAGE_ACCOUNT_NAME,
                                        container_name=CONTAINER_NAME,
                                        blob_name=blob_name,
                                        account_key=STORAGE_ACCOUNT_KEY,
                                        permission=BlobSasPermissions(read=True),
                                        expiry=datetime.utcnow() + timedelta(hours=1))  # Token valid for 1 hour
            citation['url']=citation['url']+f'?{sas_token}'

    return json.dumps(messageObj)
