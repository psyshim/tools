"""HTTP adversarial scenarios for the mock agent contract."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from coding_agent.fault_service import handler_factory

class ContractFaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1",0),handler_factory("agent-token","control-token"))
        cls.thread = threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:" + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def call(self,path,body=None,token="agent-token",key="request-1"):
        headers = {"Authorization":"Bearer "+token,"Idempotency-Key":key}
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base+path,headers=headers,data=data)
        try:
            with urllib.request.urlopen(req,timeout=2) as response:
                return response.status,json.load(response)
        except urllib.error.HTTPError as error:
            return error.code,json.load(error)

    def setUp(self):
        self.assertEqual(self.call("/__test/scenario",{"scenario":"normal","reset":True},token="control-token")[0],200)
        self.task = {"protocol":"1.0","mode":"read","workspace_id":"w1","instruction":"inspect"}

    def test_auth_and_control_isolation(self):
        self.assertEqual(self.call("/v1/tasks",self.task,token="wrong")[0],401)
        self.assertEqual(self.call("/__test/scenario",{"scenario":"unavailable"})[0],401)

    def test_idempotency_and_conflict(self):
        first=self.call("/v1/tasks",self.task)
        repeat=self.call("/v1/tasks",self.task)
        self.assertEqual(first[1]["task_id"],repeat[1]["task_id"])
        self.assertEqual(self.call("/v1/tasks",dict(self.task,instruction="different"))[0],409)

    def test_event_replay_and_expired(self):
        self.call("/__test/scenario",{"scenario":"event_replay"},token="control-token")
        task_id=self.call("/v1/tasks",self.task)[1]["task_id"]
        events=self.call(f"/v1/tasks/{task_id}/events?after=0")[1]["events"]
        self.assertEqual([item["id"] for item in events],["1","1","2"])
        self.call("/__test/scenario",{"scenario":"cursor_expired"},token="control-token")
        task_id=self.call("/v1/tasks",self.task)[1]["task_id"]
        self.assertEqual(self.call(f"/v1/tasks/{task_id}/events?after=0")[0],410)

    def test_failure_and_unavailable(self):
        self.call("/__test/scenario",{"scenario":"fail_task"},token="control-token")
        task_id=self.call("/v1/tasks",self.task)[1]["task_id"]
        self.assertEqual(self.call(f"/v1/tasks/{task_id}")[1]["state"],"FAILED")
        self.call("/__test/scenario",{"scenario":"unavailable"},token="control-token")
        self.assertEqual(self.call("/v1/tasks",self.task)[0],503)

    def test_bad_inputs(self):
        self.assertEqual(self.call("/v1/tasks",dict(self.task,mode="execute"))[0],400)
        self.assertEqual(self.call("/v1/tasks",self.task,key="")[0],400)
        self.assertEqual(self.call("/v1/tasks",self.task,token="control-token")[0],401)

if __name__ == "__main__":
    unittest.main()
