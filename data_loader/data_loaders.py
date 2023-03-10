import json, pickle
import math, random
import torch
import numpy as np

from pathlib import Path
from torch.utils.data import Dataset, DataLoader
from torch.utils.data.dataloader import default_collate

class PolyvoreDataLoader(DataLoader):
    def __init__(
        self, 
        split: str,
        data_dir: str, 
        is_disjoint: bool, 
        categories: list,
        masked_ratio: float,
        batch_size: int = 16, 
        shuffle: bool = True, 
        num_workers: int = 1
    ):
        self.categories = categories
        self.polyvore_dataset = PolyvoreDataset(data_dir, is_disjoint, split, categories, masked_ratio)

        self.init_kwargs = {
            'batch_size': batch_size,
            'shuffle': shuffle,
            'collate_fn': default_collate,
            'num_workers': num_workers
        }
        super().__init__(dataset=self.polyvore_dataset, **self.init_kwargs)


    def query_top_items(self, embeddings: torch.Tensor, device, top_k: int = 5):
        return self.polyvore_dataset.query_top_items(embeddings, device, top_k)


'''
self.index_names:  list[names]
self.index_embeddings: list[embeddings]
self.index_metadatas: dict[index, metadatas], some names will not have metadata.
self.outfits: list[dict[outfits. item_indices]]
'''
class PolyvoreDataset(Dataset):
    def __init__(self, data_dir: str, is_disjoint: bool, split: str, categories: list, masked_ratio: float):
        self.data_dir: Path = Path(data_dir).resolve()
        self.categories = categories
        self.masked_ratio = masked_ratio

        self.__load_index_names()
        self.__load_index_embeddings()
        self.__load_index_metadatas()        
        self.__load_outfits(is_disjoint, split)


    def __load_index_names(self):
        with open(self.data_dir / f"polyvore_index_names.pkl", "rb") as f:
            self.index_names = np.array(pickle.load(f))


    def __load_index_embeddings(self):
        self.index_embeddings = (
            torch.load(self.data_dir / "polyvore_index_embeddings.pt", map_location="cpu")
            .type(torch.float32)
        )
        self.index_embeddings = torch.nn.functional.normalize(self.index_embeddings)


    def __load_index_metadatas(self):
        self.index_metadatas: dict = {}

        with open(self.data_dir / "polyvore_item_metadata.json", "r") as f:
            data = json.load(f)

        for idx, name in enumerate(self.index_names):
            if name in data:
                category = data[name]["semantic_category"]
                if category in self.categories:
                    self.index_metadatas[idx] = { "category": category }


    def __load_outfits(self, is_disjoint: bool, split: str):
        if is_disjoint:
            outfits_dir = "disjoint"
        else:
            outfits_dir = "nondisjoint"

        # Read outfits from file
        self.outfits: list = []
        with open(self.data_dir / outfits_dir / f"{split}.json", "r") as f:
            data = json.load(f)
        
        # Create an inversed dictionary for faster lookup
        temp_dict = dict((names, idx) for idx, names in enumerate(self.index_names) if idx in self.index_metadatas)

        # Traverse the data in file
        for outfit in data:
            set_id = outfit["set_id"]
            items = outfit["items"]

            valid_items = [item for item in items if item in temp_dict]
            if len(valid_items) > 1:
                self.outfits.append({ "set_id": set_id, "items": [temp_dict[i] for i in valid_items] })


    def __getitem__(self, index):
        n = len(self.categories)
        item_indices = torch.full((n,), -1).int()
        embeddings = torch.zeros(n, self.index_embeddings.shape[-1])
        input_mask = torch.full((n,), False) 
        target_mask = torch.full((n,), False) 

        # Read embeddings
        outfit = self.outfits[index]
        for item_idx in outfit["items"]:
            embedding = self.index_embeddings[item_idx]

            category = self.index_metadatas[item_idx]["category"] 
            category_idx = self.categories.index(category)

            item_indices[category_idx] = item_idx
            embeddings[category_idx] = embedding
            input_mask[category_idx] = True

        # Mask partial to create target list
        available_idx = [idx for idx, i in enumerate(input_mask) if i]
        random.shuffle(available_idx)

        n_item_masked = math.ceil(self.masked_ratio * len(available_idx))
        n_item_masked = min(len(available_idx) - 1, max(1, n_item_masked))
        for i in available_idx[:n_item_masked]:
            input_mask[i] = False
            target_mask[i] = True

        return item_indices, embeddings, input_mask, target_mask


    def __len__(self):
        return len(self.outfits)


    def query_top_items(self, embeddings: torch.Tensor, device, top_k: int = 5):
        dim = [i for i in embeddings.shape]
        _embeddings = embeddings.reshape(-1, dim[-1])

        dataset_embeddings = self.index_embeddings.to(device)
        cos_similarity = _embeddings @ dataset_embeddings.transpose(0, 1)
        sorted_indices = torch.topk(cos_similarity, top_k, dim=1, largest=True).indices

        # Trick to reshape to original batch_size and num_categories
        dim[-1] = top_k
        return torch.reshape(sorted_indices, tuple(dim))
