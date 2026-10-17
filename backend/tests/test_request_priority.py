from app.request_priority import format_prioritized_input, parse_prioritized_requests


def test_priority_prefixes_sort_requests_and_keep_stable_order():
    parsed = parse_prioritized_requests(
        "! Polish animation\n!!! Fix login\nUnmarked request\n!! Add loading state\n!!! Restore access"
    )

    assert [(item.text, item.level) for item in parsed] == [
        ("Fix login", 4),
        ("Restore access", 4),
        ("Add loading state", 3),
        ("Unmarked request", 2),
        ("Polish animation", 1),
    ]


def test_exclamation_is_only_a_prefix_when_followed_by_whitespace():
    parsed = parse_prioritized_requests("Great! Keep this\n!!!urgent is plain text")
    assert [item.text for item in parsed] == ["Great! Keep this", "!!!urgent is plain text"]
    assert all(item.level == 2 for item in parsed)


def test_formatted_input_explains_priority_to_the_model():
    assert format_prioritized_input("! Polish\n!!! Fix") == (
        "[MUST COMPLETE] Fix\n[NICE TO HAVE] Polish"
    )


def test_unmarked_input_is_not_rewritten():
    raw = "Add login\nInclude password reset"
    assert format_prioritized_input(raw) == raw
