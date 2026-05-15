class AdapterDeliveryError(Exception):
    def __init__(self, status: int | str, body: str = ""):
        body = body or ""
        self.status = status
        self.body = body
        super().__init__(f"adapter delivery failed: status={status} body={body[:200]}")
