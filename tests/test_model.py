import io
import json
import unittest
from unittest.mock import patch
from model import ModelError, Ollama


class ModelContractTests(unittest.TestCase):
    def test_generate_wire_contract(self):
        data = {"response": "answer", "done": True, "prompt_eval_count": 3, "eval_count": 2}
        with patch("model.urlopen", return_value=io.BytesIO(json.dumps(data).encode())) as transport:
            result = Ollama("test-model").complete("hello", max_tokens=32)
        request = transport.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "http://localhost:11434/api/generate")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["options"]["num_predict"], 32)
        self.assertEqual(result["input_tokens"], 3)

    def test_incomplete_response_is_rejected(self):
        with patch.object(Ollama, "_post", return_value={"response": "partial", "done": False}):
            with self.assertRaises(ModelError):
                Ollama("test").complete("hello")

    def test_malformed_embedding_is_rejected(self):
        with patch.object(Ollama, "_post", return_value={"embeddings": [[1, 2], [3]]}):
            with self.assertRaises(ModelError):
                Ollama("test").embed(["one", "two"])

    def test_structured_output_must_be_object(self):
        with patch.object(Ollama, "complete", return_value={"text": "[]"}):
            with self.assertRaises(ModelError):
                Ollama("test").json("hello")


if __name__ == "__main__":
    unittest.main()
