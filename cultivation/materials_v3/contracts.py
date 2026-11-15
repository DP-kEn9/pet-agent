from datetime import datetime, date, timedelta
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, AwareDatetime, model_validator
from pydantic_core import PydanticCustomError
import hashlib, json
from uuid import uuid5, NAMESPACE_URL

VERSION = '4.0.1'

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def identity(*parts):
    return str(uuid5(NAMESPACE_URL, ':'.join(map(str,parts))))

class V3Error(Exception):
    def __init__(self, code, message, *, state='FAILED', affected=None, retryable=False):
        self.code,self.message,self.state,self.affected,self.retryable=code,message,state,affected or [],retryable
    def public(self):
        return dict(code=self.code,message=self.message,retryable=self.retryable,fieldErrors=[],
                    currentRevisions={},actionRequired=self.state if self.state!='FAILED' else None,affectedIds=self.affected,traceId=__import__('uuid').uuid4().hex)

class Budget(Strict):
    bytes: StrictInt = Field(default=64*1024*1024,ge=0,le=512*1024*1024)
    pages: StrictInt = Field(default=20,ge=0,le=10000)
    modelCalls: StrictInt = Field(default=10,ge=0,le=1000)
    ocrPages: StrictInt = Field(default=0,ge=0,le=10000)
    entries: StrictInt = Field(default=10000,ge=1,le=10000)
