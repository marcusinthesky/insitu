"""An actual Python extension used by both protocol and native-host tests."""

from insitu.sdk import Context, Extension
from insitu.types import Diagnostic, Severity
from pydantic import BaseModel

app = Extension(name="conformance", version="1.0.0")


class Greeting(BaseModel):
    name: str = "world"


@app.transform("greeting", params=Greeting)
def greeting(options: Greeting, context: Context) -> str:
    return f"Hello, {options.name}!"


@app.transform("read-file")
async def read_file(options, context: Context) -> str:
    return str(await context.resource("fs:file:" + options.get("path", "snippet.txt")))


@app.resource("numbers", result=list[int])
def numbers(options, context):
    return [1, 2, 3]


@app.renderer("join", value=list[int])
def join(value, options, context):
    return ",".join(map(str, value))


@app.validator("notice")
def notice(options, context):
    return (Diagnostic(severity=Severity.INFO, message="extension validated"),)


if __name__ == "__main__":
    app.run()
