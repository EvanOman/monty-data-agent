import pytest


@pytest.mark.asyncio
async def test_sqlite_conversation_lifecycle(sqlite_store):
    # Create conversation
    conv = await sqlite_store.create_conversation("Test chat")
    assert conv["title"] == "Test chat"
    cid = conv["id"]

    # List conversations
    convs = await sqlite_store.list_conversations()
    assert len(convs) == 1
    assert convs[0]["id"] == cid

    # Add messages
    await sqlite_store.add_message(cid, "user", "Hello")
    m2 = await sqlite_store.add_message(cid, "assistant", "Hi there")

    # Get messages
    msgs = await sqlite_store.get_messages(cid)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"

    # Save artifact
    art = await sqlite_store.save_artifact(
        conversation_id=cid,
        message_id=m2["id"],
        code='sql("SELECT 1")',
        result_json='[{"1": 1}]',
        result_type="table",
    )
    assert art["code"] == 'sql("SELECT 1")'

    # Get artifact
    fetched = await sqlite_store.get_artifact(art["id"])
    assert fetched is not None
    assert fetched["result_type"] == "table"

    # Get artifacts for conversation
    arts = await sqlite_store.get_artifacts_for_conversation(cid)
    assert len(arts) == 1


@pytest.mark.asyncio
async def test_conversation_title_update(sqlite_store):
    conv = await sqlite_store.create_conversation()
    assert conv["title"] == "New conversation"

    await sqlite_store.update_conversation_title(conv["id"], "Updated title")
    updated = await sqlite_store.get_conversation(conv["id"])
    assert updated["title"] == "Updated title"
