from vsummary.util.discord import split_message


def test_split_message_respects_limit_and_preserves_content():
    text = "a" * 1999 + "\n" + "b" * 10

    chunks = split_message(text)

    assert chunks == ["a" * 1999, "b" * 10]
    assert all(0 < len(chunk) <= 2000 for chunk in chunks)


def test_split_message_handles_long_lines_and_exact_boundary():
    text = "x" * 2000 + "y"

    assert split_message(text) == ["x" * 2000, "y"]


def test_split_message_never_emits_empty_chunks():
    assert split_message("\n" * 5 + "hello") == ["hello"]
    assert split_message("") == []
