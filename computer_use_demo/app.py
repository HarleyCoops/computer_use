"""
Entrypoint for Gradio, see https://gradio.app/
"""

import asyncio
import base64
import os
import subprocess
from datetime import datetime
from enum import StrEnum
from functools import partial
from pathlib import PosixPath
from typing import cast, Dict

import gradio as gr
from anthropic import APIResponse
from anthropic.types import TextBlock
from anthropic.types.beta import BetaMessage, BetaTextBlock, BetaToolUseBlock
from anthropic.types.tool_use_block import ToolUseBlock

from computer_use_demo.loop import (
    PROVIDER_TO_DEFAULT_MODEL_NAME,
    APIProvider,
    sampling_loop,
)

from computer_use_demo.tools import ToolResult


CONFIG_DIR = PosixPath("~/.anthropic").expanduser()
API_KEY_FILE = CONFIG_DIR / "api_key"

WARNING_TEXT = "⚠️ Security Alert: Never provide access to sensitive accounts or data, as malicious web content can hijack Claude's behavior"


class Sender(StrEnum):
    USER = "user"
    BOT = "assistant"
    TOOL = "tool"


def setup_state(state):
    if "messages" not in state:
        state["messages"] = []
    if "api_key" not in state:
        # Try to load API key from file first, then environment
        state["api_key"] = load_from_storage("api_key") or os.getenv("ANTHROPIC_API_KEY", "")
        if not state["api_key"]:
            state["api_key"] = "YOUR_API_KEY_HERE"
            print("API key not found. Please set it in the environment or storage.")
    if "provider" not in state:
        state["provider"] = os.getenv("API_PROVIDER", "anthropic") or APIProvider.ANTHROPIC
    if "provider_radio" not in state:
        state["provider_radio"] = state["provider"]
    if "model" not in state:
        _reset_model(state)
    if "auth_validated" not in state:
        state["auth_validated"] = False
    if "responses" not in state:
        state["responses"] = {}
    if "tools" not in state:
        state["tools"] = {}
    if "only_n_most_recent_images" not in state:
        state["only_n_most_recent_images"] = 10
    if "custom_system_prompt" not in state:
        state["custom_system_prompt"] = load_from_storage("system_prompt") or ""
    if "hide_images" not in state:
        state["hide_images"] = False


def _reset_model(state):
    state["model"] = PROVIDER_TO_DEFAULT_MODEL_NAME[cast(APIProvider, state["provider"])]


async def main(state):
    """Render loop for Gradio"""
    setup_state(state)
    return "Setup completed"


def validate_auth(provider: APIProvider, api_key: str | None):
    if provider == APIProvider.ANTHROPIC:
        if not api_key:
            return "Enter your Anthropic API key to continue."
    if provider == APIProvider.BEDROCK:
        import boto3

        if not boto3.Session().get_credentials():
            return "You must have AWS credentials set up to use the Bedrock API."
    if provider == APIProvider.VERTEX:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError

        if not os.environ.get("CLOUD_ML_REGION"):
            return "Set the CLOUD_ML_REGION environment variable to use the Vertex API."
        try:
            google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        except DefaultCredentialsError:
            return "Your google cloud credentials are not set up correctly."


def load_from_storage(filename: str) -> str | None:
    """Load data from a file in the storage directory."""
    try:
        file_path = CONFIG_DIR / filename
        if file_path.exists():
            data = file_path.read_text().strip()
            if data:
                return data
    except Exception as e:
        print(f"Debug: Error loading {filename}: {e}")
    return None


def save_to_storage(filename: str, data: str) -> None:
    """Save data to a file in the storage directory."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        file_path = CONFIG_DIR / filename
        file_path.write_text(data)
        # Ensure only user can read/write the file
        file_path.chmod(0o600)
    except Exception as e:
        print(f"Debug: Error saving {filename}: {e}")


def _api_response_callback(response: APIResponse[BetaMessage], response_state: dict):
    response_id = datetime.now().isoformat()
    response_state[response_id] = response


def _tool_output_callback(tool_output: ToolResult, tool_id: str, tool_state: dict):
    tool_state[tool_id] = tool_output


def _render_message(sender: Sender, message: str | BetaTextBlock | BetaToolUseBlock | ToolResult, state):
    is_tool_result = not isinstance(message, str) and (
        isinstance(message, ToolResult)
        or message.__class__.__name__ == "ToolResult"
        or message.__class__.__name__ == "CLIResult"
    )
    if not message or (
        is_tool_result
        and state["hide_images"]
        and not hasattr(message, "error")
        and not hasattr(message, "output")
    ):
        return
    if is_tool_result:
        message = cast(ToolResult, message)
        if message.output:
            return message.output
        if message.error:
            return f"Error: {message.error}"
        if message.base64_image and not state["hide_images"]:
            return base64.b64decode(message.base64_image)
    elif isinstance(message, BetaTextBlock) or isinstance(message, TextBlock):
        return message.text
    elif isinstance(message, BetaToolUseBlock) or isinstance(message, ToolUseBlock):
        return f"Tool Use: {message.name}\nInput: {message.input}"
    else:
        return message

from PIL import Image
from io import BytesIO
def decode_base64_image(base64_str):
    # 移除base64字符串的前缀（如果存在）
    if base64_str.startswith("data:image"):
        base64_str = base64_str.split(",")[1]
    # 解码base64字符串并将其转换为PIL图像
    image_data = base64.b64decode(base64_str)
    image = Image.open(BytesIO(image_data))
    # 保存图像为screenshot.png
    import datetime
    image.save(f"{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.png")
    print("screenshot saved")
    return f"{datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.png"

def process_input(user_input, state):
    # Ensure the state is properly initialized
    setup_state(state)
    
    # Append the user input to the messages in the state
    state["messages"].append(
        {
            "role": Sender.USER,
            "content": [TextBlock(type="text", text=user_input)],
        }
    )

    # Run the sampling loop asynchronously
    asyncio.run(sampling_loop_wrapper(state))

    # Return the conversation so far
    # return [msg["content"][0]["text"] for msg in state["messages"]]
    # res = []
    # for i in range(0, len(state["messages"]), 2):
    #     try:
    #         # Create a pair from the current and next message (if it exists)
    #         pair = []
    #         if "content" in state["messages"][i]:
    #             pair.append(state["messages"][i]["content"][0].text)  # Add the first message's content
    #         if i + 1 < len(state["messages"]) and "content" in state["messages"][i + 1]:
    #             pair.append(state["messages"][i + 1]["content"][0].text)  # Add the second message's content

    #         # Add the pair to the result list if it contains at least one message
    #         if pair:
    #             res.append(pair)
    #     except Exception as e:
    #         # Handle exceptions and continue with the next pair
    #         pass
    res = []
    for msg in state["messages"]:
        try:
            if isinstance(msg["content"][0], TextBlock):
                res.append((msg["content"][0].text, None))
            elif isinstance(msg["content"][0], BetaTextBlock):
                res.append((None, msg["content"][0].text))
            elif isinstance(msg["content"][0], BetaToolUseBlock):
                res.append((None, f"Tool Use: {msg['content'][0].name}\nInput: {msg['content'][0].input}"))
            # elif isinstance(msg["content"][0], Dict) and "data" in msg["content"][0]["content"][0].keys():
            elif isinstance(msg["content"][0], Dict) and msg["content"][0]["content"][0]["type"] == "image":
                with open("D:\\msg.txt", "w") as f:
                    f.write(str(msg["content"][0]))
                # res.append((None, msg["content"][0]["text"]))
                # print(msg["content"][0]["content"][0]["data"][:100])
                image_path = decode_base64_image(msg["content"][0]["content"][0]["source"]["data"])
                res.append((None, gr.Image(image_path)))
                # res.append((None, f'The screenshot is: <img src="data:image/png;base64,{msg["content"][0]["content"][0]["data"]}">'))
            else:
                # res.append((None, f'The screenshot is: <img src="data:image/png;base64,{msg["content"][0]["content"][0]["data"]}">'))
                # res.append(msg["content"][0])
                print(msg["content"][0])
        except Exception as e:
            print("error", e)
            pass
            # print(msg["content"])
    return res


async def sampling_loop_wrapper(state):
    # Ensure the API key is present
    if not state.get("api_key"):
        raise ValueError("API key is missing. Please set it in the environment or storage.")
    
    await sampling_loop(
        system_prompt_suffix=state["custom_system_prompt"],
        model=state["model"],
        provider=state["provider"],
        messages=state["messages"],
        output_callback=partial(_render_message, Sender.BOT, state=state),
        tool_output_callback=partial(_tool_output_callback, tool_state=state["tools"]),
        api_response_callback=partial(_api_response_callback, response_state=state["responses"]),
        api_key=state["api_key"],  # Pass the API key here
        only_n_most_recent_images=state["only_n_most_recent_images"],
    )


with gr.Blocks() as demo:
    state = {}

    gr.Markdown("# Claude Computer Use Demo")

    if not os.getenv("HIDE_WARNING", False):
        gr.Markdown(WARNING_TEXT)

    provider = gr.Dropdown(
        label="API Provider",
        choices=[option.value for option in APIProvider],
        value="anthropic",
        interactive=True,
    )
    model = gr.Textbox(label="Model", value="claude-3-5-sonnet-20241022")
    api_key = gr.Textbox(
        label="Anthropic API Key",
        type="password",
        value="",
        interactive=True,
    )
    only_n_images = gr.Slider(
        label="Only send N most recent images",
        minimum=0,
        value=10,
        interactive=True,
    )
    custom_prompt = gr.Textbox(
        label="Custom System Prompt Suffix",
        value="",
        interactive=True,
    )
    hide_images = gr.Checkbox(label="Hide screenshots", value=False)

    chat_input = gr.Textbox(label="Type a message to send to Claude...")
    # chat_output = gr.Textbox(label="Chat Output", interactive=False)
    chatbot = gr.Chatbot(label="Chatbot History")

    chat_input.submit(process_input, [chat_input, state], chatbot)

demo.launch()
