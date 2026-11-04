import pytest

from app.requirement_feedback import generation_failure_message


@pytest.mark.parametrize('detail, expected', [
    ('context_length_exceeded: maximum context length', 'context limit'),
    ('gatherer timed out', 'timed out'),
    ('finish_reason=length', 'output token limit'),
    ('empty model output', 'no usable output'),
    ('invalid JSON', 'valid requirement data'),
])
def test_failure_reason_is_specific_and_preserves_saved_state(detail, expected):
    message = generation_failure_message(RuntimeError(detail))
    assert expected in message
    assert 'saved requirements are unchanged' in message


def test_nested_context_error_is_not_misreported_as_parse_error():
    try:
        try:
            raise ValueError('maximum context length exceeded')
        except ValueError as error:
            raise RuntimeError('All parsing attempts failed') from error
    except RuntimeError as error:
        assert 'context limit' in generation_failure_message(error)


def test_unknown_error_does_not_claim_context_is_full_or_leak_details():
    message = generation_failure_message(RuntimeError('private provider details'))
    assert 'context limit' not in message
    assert 'private provider details' not in message
