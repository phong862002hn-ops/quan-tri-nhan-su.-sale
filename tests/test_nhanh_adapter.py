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
        self.assertEqual(conversation["channel"], "nhanh_vpage")
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


if __name__ == "__main__":
    unittest.main()
