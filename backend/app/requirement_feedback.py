"""Safe, evidence-based messages for unsuccessful requirement generation."""


def generation_failure_message(error: Exception) -> str:
    details = []
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        details.append(str(current).casefold())
        current = current.__cause__ or current.__context__
    text = ' '.join(details)
    if any(term in text for term in (
        'context length', 'context window', 'context_length_exceeded',
        'maximum context', 'too many tokens', 'context limit',
    )):
        reason = 'The request exceeds the model context limit. Shorten the request or increase the loaded model context in LM Studio.'
    elif 'timed out' in text or 'timeout' in text:
        reason = 'The model timed out before producing a usable result. Check LM Studio and retry.'
    elif 'output token limit' in text or 'finish_reason=length' in text:
        reason = 'The model response reached its output token limit before completing. Increase the output budget or request a smaller change.'
    elif 'empty model output' in text or 'no structured requirement result' in text:
        reason = 'The model returned no usable output. Check that a model is loaded in LM Studio and retry.'
    else:
        reason = 'The model response could not be read as valid requirement data. Retry with the requirement ID and the exact change.'
    return f'No requirement update was prepared. {reason} Your saved requirements are unchanged.'
