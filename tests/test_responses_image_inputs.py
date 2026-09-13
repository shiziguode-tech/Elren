import copy

import pytest

from deepdesk.deepseek import DeepSeekClient


def payload(content):
    client = DeepSeekClient('https://deepseek.example', 'deepseek-v4-flash', '', '')
    client.set_provider_models([], {'openai': 'synthetic'})
    return client._openai_responses_payload(
        client._endpoint_for('aicodemirror-openai:gpt-6-astra'),
        [{'role': 'user', 'content': content}], [], 512, 'medium')


@pytest.mark.parametrize('url', ['data:image/png;base64,eA==', 'https://example.com/image.png'])
def test_chat_images_preserve_order_detail_and_do_not_mutate_history(url):
    message = [{'type': 'text', 'text': 'first'},
               {'type': 'image_url', 'image_url': {'url': url, 'detail': 'high'}},
               {'type': 'text', 'text': 'second'},
               {'type': 'input_image', 'file_id': 'file-synthetic', 'detail': 'auto'}]
    before = copy.deepcopy(message)
    result = payload(message)
    assert result['input'][0]['content'] == [
        {'type': 'input_text', 'text': 'first'},
        {'type': 'input_image', 'image_url': url, 'detail': 'high'},
        {'type': 'input_text', 'text': 'second'},
        {'type': 'input_image', 'file_id': 'file-synthetic', 'detail': 'auto'}]
    assert result['reasoning'] == {'effort': 'medium'}
    assert message == before


def test_image_only_message_does_not_turn_into_continue():
    assert payload([{'type': 'image_url', 'image_url': 'https://example.com/a.png'}])['input'] == [
        {'role': 'user', 'content': [{'type': 'input_image', 'image_url': 'https://example.com/a.png'}]}]


@pytest.mark.parametrize('part', [
    {'type': 'image_url', 'image_url': {}},
    {'type': 'image_url', 'image_url': {'url': '', 'detail': 'high'}},
    {'type': 'image_url', 'image_url': {'url': 'https://example.com/a.png', 'detail': 'invalid'}},
    {'type': 'input_image'},
    {'type': 'input_image', 'image_url': 'url', 'file_id': 'id'},
    {'type': 'input_image', 'file_id': 123},
])
def test_invalid_images_raise_instead_of_silently_disappearing(part):
    with pytest.raises(ValueError):
        payload([part])


def test_plain_text_transcripts_remain_unchanged():
    assert payload([{'type': 'text', 'text': 'hello'}])['input'] == [{'role': 'user', 'content': 'hello'}]
