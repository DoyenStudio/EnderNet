# coding=utf-8
"""Item selector catalogue protocol; no engine imports."""


def build_event_classes(events):
    class CatalogRequest(events.CustomC2SEvent):
        name = "EnderNet:ItemCatalogRequest"

        def __init__(self, player_id=None):
            self.player_id = player_id

        def marshal(self):
            return {}

        @classmethod
        def unmarshal(cls, data):
            return cls(data.get("__id__"))

    class CatalogResponse(events.CustomS2CEvent):
        name = "EnderNet:ItemCatalogResponse"

        def __init__(self, item_ids, fluid_ids=None, bucket_fluid_ids=None):
            self.item_ids = item_ids
            self.fluid_ids = fluid_ids if fluid_ids is not None else []
            self.bucket_fluid_ids = bucket_fluid_ids if bucket_fluid_ids is not None else {}

        def marshal(self):
            return {"item_ids": self.item_ids, "fluid_ids": self.fluid_ids,
                    "bucket_fluid_ids": self.bucket_fluid_ids}

        @classmethod
        def unmarshal(cls, data):
            return cls(data.get("item_ids", []), data.get("fluid_ids", []),
                       data.get("bucket_fluid_ids", {}))

    return CatalogRequest, CatalogResponse
