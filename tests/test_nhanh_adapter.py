import unittest

from app.nhanh_adapter import build_conversation, map_live_conversation


class NhanhAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.summary = {
            "id": "100214446269995_24497024349966605",
            "type": 2,
            "pageUserName": "PhLe Vyy",
            "pageUserId": "24497024349966605",
            "pageId": "100214446269995",
            "createdAt": 1778420859,
            "updatedAt": 1778497586,
        }
        self.payload = {
            "code": 1,
            "items": [
                {
                    "id": "m_employee",
                    "conversationId": "100214446269995_24497024349966605",
                    "senderId": "100214446269995",
                    "senderName": "Thuốc nhuộm tóc BuddyHairs",
                    "message": "Dạ nâng nền của shop 100k/set nha",
                    "attachments": [
                        {
                            "type": "image",
                            "payload": {"url": "https://example.com/image.jpg"},
                        }
                    ],
                    "createdAt": 1778497552,
                    "pageUserId": "24497024349966605",
                    "pageId": "100214446269995",
                },
                {
                    "id": "m_customer",
                    "conversationId": "100214446269995_24497024349966605",
                    "senderId": "24497024349966605",
                    "senderName": "PhLe Vyy",
                    "message": "cho em hỏi nếu mua kèm nâng nền thì nhiêu á shop",
                    "createdAt": 1778487490,
                    "pageUserId": "24497024349966605",
                    "pageId": "100214446269995",
                },
            ],
        }

    def test_map_live_conversation_sets_roles_and_metadata(self) -> None:
        conversation = map_live_conversation(
            "100214446269995_24497024349966605",
            self.payload,
            summary_item=self.summary,
        )
        self.assertEqual(conversation["external_id"], "100214446269995_24497024349966605")
        self.assertEqual(conversation["channel"], "nhanh_facebook")
        self.assertEqual(conversation["metadata"]["channel_label"], "Facebook")
        self.assertEqual(conversation["employee"]["id"], "page_100214446269995")
        self.assertEqual(conversation["metadata"]["customer_name"], "PhLe Vyy")
        self.assertEqual(conversation["messages"][0]["sender_type"], "customer")
        self.assertEqual(conversation["messages"][1]["sender_type"], "employee")

    def test_build_conversation_returns_schema_object(self) -> None:
        conversation = build_conversation(
            "100214446269995_24497024349966605",
            self.payload,
            summary_item=self.summary,
        )
        self.assertEqual(conversation.external_id, "100214446269995_24497024349966605")
        self.assertEqual(len(conversation.messages), 2)
        self.assertEqual(conversation.messages[1].attachments[0].type, "image")
        self.assertTrue(conversation.messages[0].sent_at)


    def test_channel_label_from_code(self) -> None:
        from app.nhanh_adapter import channel_label
        self.assertEqual(channel_label(1, ""), "Facebook")
        self.assertEqual(channel_label(4, ""), "Shopee")
        self.assertEqual(channel_label(9, ""), "TikTok")
        self.assertEqual(channel_label(2, ""), "Instagram")

    def test_channel_label_fallback_from_page_id(self) -> None:
        from app.nhanh_adapter import channel_label
        self.assertEqual(channel_label(None, "sp_302719943"), "Shopee")
        self.assertEqual(channel_label(None, "tt_7494568"), "TikTok")
        self.assertEqual(channel_label(None, "100214446269995"), "Facebook")
        self.assertEqual(channel_label(None, "weird_xyz"), "Unknown")

    def test_admin_message_not_from_page_id_treated_as_employee(self) -> None:
        summary = {"id": "conv_1", "pageId": "PAGE_123", "pageUserId": "CUSTOMER_456"}
        payload = {"items": [
            {"id": "m1", "senderId": "ADMIN_OTHER_789", "message": "dạ chào mình", "createdAt": 1778420800},
            {"id": "m2", "senderId": "CUSTOMER_456", "message": "shop ơi", "createdAt": 1778420850},
            {"id": "m3", "senderId": "PAGE_123", "message": "dạ có ạ", "createdAt": 1778420900},
        ]}
        result = map_live_conversation("conv_1", payload, summary_item=summary)
        roles = {m["id"]: m["sender_type"] for m in result["messages"]}
        self.assertEqual(roles["m1"], "employee")
        self.assertEqual(roles["m2"], "customer")
        self.assertEqual(roles["m3"], "employee")

    def test_attachment_metadata_preserved(self) -> None:
        conversation = build_conversation(
            "100214446269995_24497024349966605",
            self.payload,
            summary_item=self.summary,
        )
        employee_msg = next(m for m in conversation.messages if m.sender_type == "employee")
        self.assertEqual(employee_msg.attachments[0].type, "image")
        self.assertIn("raw_payload", employee_msg.attachments[0].metadata)
        self.assertEqual(
            employee_msg.attachments[0].metadata["raw_payload"]["url"],
            "https://example.com/image.jpg",
        )

    def test_messages_sorted_by_time_regardless_of_input_order(self) -> None:
        summary = {"id": "c1", "pageId": "PAGE", "pageUserId": "USR"}
        payload = {"items": [
            {"id": "m3", "senderId": "USR", "message": "third", "createdAt": 1778420900},
            {"id": "m1", "senderId": "USR", "message": "first", "createdAt": 1778420800},
            {"id": "m2", "senderId": "PAGE", "message": "second", "createdAt": 1778420850},
        ]}
        result = map_live_conversation("c1", payload, summary_item=summary)
        self.assertEqual([m["text"] for m in result["messages"]], ["first", "second", "third"])

    def test_message_with_missing_timestamp_does_not_crash(self) -> None:
        summary = {"id": "c1", "pageId": "PAGE", "pageUserId": "USR"}
        payload = {"items": [
            {"id": "m1", "senderId": "USR", "message": "no time"},
            {"id": "m2", "senderId": "PAGE", "message": "has time", "createdAt": 1778420800},
        ]}
        result = map_live_conversation("c1", payload, summary_item=summary)
        self.assertEqual(len(result["messages"]), 2)
        # has-time first (real timestamp), no-time pushed to end (sentinel "9999")
        self.assertEqual(result["messages"][0]["text"], "has time")
        self.assertEqual(result["messages"][1]["text"], "no time")


if __name__ == "__main__":
    unittest.main()
