from functools import partial
import json
import os
from pathlib import Path
import re
import io
from typing import Any, Callable, Optional, Sequence, Iterable
from typing import Dict
from typing import List
from typing import Union
import soundfile

import numpy as np
from torch.utils.data import SequentialSampler
from torch.utils.data.distributed import DistributedSampler
import webdataset as wds

from ..core import AudioSignal
from ..core import util


class AudioLoader:
    """Loads audio endlessly from a list of audio sources
    containing paths to audio files. Audio sources can be
    folders full of audio files (which are found via file
    extension) or by providing a CSV file which contains paths
    to audio files.

    Parameters
    ----------
    sources : List[str], optional
        Sources containing folders, or CSVs with
        paths to audio files, by default None
    weights : List[float], optional
        Weights to sample audio files from each source, by default None
    relative_path : str, optional
        Path audio should be loaded relative to, by default ""
    transform : Callable, optional
        Transform to instantiate alongside audio sample,
        by default None
    ext : List[str]
        List of extensions to find audio within each source by. Can
        also be a file name (e.g. "vocals.wav"). by default
        ``['.wav', '.flac', '.mp3', '.mp4']``.
    shuffle: bool
        Whether to shuffle the files within the dataloader. Defaults to True.
    shuffle_state: int
        State to use to seed the shuffle of the files.
    """

    def __init__(
        self,
        sources: List[str] = None,
        weights: List[float] = None,
        transform: Callable = None,
        relative_path: str = "",
        ext: List[str] = util.AUDIO_EXTENSIONS,
        shuffle: bool = True,
        shuffle_state: int = 0,
    ):
        self.audio_lists = util.read_sources(
            sources, relative_path=relative_path, ext=ext
        )

        self.audio_indices = [
            (src_idx, item_idx)
            for src_idx, src in enumerate(self.audio_lists)
            for item_idx in range(len(src))
        ]
        if shuffle:
            state = util.random_state(shuffle_state)
            state.shuffle(self.audio_indices)

        self.sources = sources
        self.weights = weights
        self.transform = transform

    def __call__(
        self,
        state,
        sample_rate: int,
        duration: float,
        loudness_cutoff: float = -40,
        num_channels: int = 1,
        offset: float = None,
        source_idx: int = None,
        item_idx: int = None,
        global_idx: int = None,
    ):
        if source_idx is not None and item_idx is not None:
            try:
                audio_info = self.audio_lists[source_idx][item_idx]
            except:
                audio_info = {"path": "none"}
        elif global_idx is not None:
            source_idx, item_idx = self.audio_indices[
                global_idx % len(self.audio_indices)
            ]
            audio_info = self.audio_lists[source_idx][item_idx]
        else:
            audio_info, source_idx, item_idx = util.choose_from_list_of_lists(
                state, self.audio_lists, p=self.weights
            )

        path = audio_info["path"]
        signal = AudioSignal.zeros(duration, sample_rate, num_channels)

        if path != "none":
            if offset is None:
                try:
                    signal = AudioSignal.salient_excerpt(
                        path,
                        duration=duration,
                        state=state,
                        loudness_cutoff=loudness_cutoff,
                    )
                except (RuntimeError, soundfile.LibsndfileError) as e:
                    if (
                        isinstance(e, soundfile.LibsndfileError)
                        or "The size of tensor a (5) must match the size of tensor b (6) at non-singleton dimension 1"
                        in str(e)
                        or "is empty!" in str(e)
                    ):
                        print(f"Error loading audio at {path}. Skipping...")
                        with open("/tmp/corrupt.txt", "a+") as file:
                            try:
                                file.write(f"{path}\n")
                            except UnicodeEncodeError:
                                pass
                    else:
                        raise e
            else:
                signal = AudioSignal(
                    path,
                    offset=offset,
                    duration=duration,
                )

        if num_channels == 1:
            signal = signal.to_mono()
        signal = signal.resample(sample_rate)

        if signal.duration < duration:
            signal = signal.zero_pad_to(int(duration * sample_rate))

        for k, v in audio_info.items():
            signal.metadata[k] = v

        item = {
            "signal": signal,
            "source_idx": source_idx,
            "item_idx": item_idx,
            "source": str(self.sources[source_idx]),
            "path": str(path),
        }
        if self.transform is not None:
            item["transform_args"] = self.transform.instantiate(state, signal=signal)
        return item


def default_matcher(x, y):
    return Path(x).parent == Path(y).parent


def align_lists(lists, matcher: Callable = default_matcher):
    longest_list = lists[np.argmax([len(l) for l in lists])]
    for i, x in enumerate(longest_list):
        for l in lists:
            if i >= len(l):
                l.append({"path": "none"})
            elif not matcher(l[i]["path"], x["path"]):
                l.insert(i, {"path": "none"})
    return lists


class AudioDataset:
    """Loads audio from multiple loaders (with associated transforms)
    for a specified number of samples. Excerpts are drawn randomly
    of the specified duration, above a specified loudness threshold
    and are resampled on the fly to the desired sample rate
    (if it is different from the audio source sample rate).

    This takes either a single AudioLoader object,
    a dictionary of AudioLoader objects, or a dictionary of AudioLoader
    objects. Each AudioLoader is called by the dataset, and the
    result is placed in the output dictionary. A transform can also be
    specified for the entire dataset, rather than for each specific
    loader. This transform can be applied to the output of all the
    loaders if desired.

    AudioLoader objects can be specified as aligned, which means the
    loaders correspond to multitrack audio (e.g. a vocals, bass,
    drums, and other loader for multitrack music mixtures).


    Parameters
    ----------
    loaders : Union[AudioLoader, List[AudioLoader], Dict[str, AudioLoader]]
        AudioLoaders to sample audio from.
    sample_rate : int
        Desired sample rate.
    n_examples : int, optional
        Number of examples (length of dataset), by default 1000
    duration : float, optional
        Duration of audio samples, by default 0.5
    loudness_cutoff : float, optional
        Loudness cutoff threshold for audio samples, by default -40
    num_channels : int, optional
        Number of channels in output audio, by default 1
    transform : Callable, optional
        Transform to instantiate alongside each dataset item, by default None
    aligned : bool, optional
        Whether the loaders should be sampled in an aligned manner (e.g. same
        offset, duration, and matched file name), by default False
    shuffle_loaders : bool, optional
        Whether to shuffle the loaders before sampling from them, by default False
    matcher : Callable
        How to match files from adjacent audio lists (e.g. for a multitrack audio loader),
        by default uses the parent directory of each file.
    without_replacement : bool
        Whether to choose files with or without replacement, by default True.


    Examples
    --------
    >>> from audiotools.data.datasets import AudioLoader
    >>> from audiotools.data.datasets import AudioDataset
    >>> from audiotools import transforms as tfm
    >>> import numpy as np
    >>>
    >>> loaders = [
    >>>     AudioLoader(
    >>>         sources=[f"tests/audio/spk"],
    >>>         transform=tfm.Equalizer(),
    >>>         ext=["wav"],
    >>>     )
    >>>     for i in range(5)
    >>> ]
    >>>
    >>> dataset = AudioDataset(
    >>>     loaders = loaders,
    >>>     sample_rate = 44100,
    >>>     duration = 1.0,
    >>>     transform = tfm.RescaleAudio(),
    >>> )
    >>>
    >>> item = dataset[np.random.randint(len(dataset))]
    >>>
    >>> for i in range(len(loaders)):
    >>>     item[i]["signal"] = loaders[i].transform(
    >>>         item[i]["signal"], **item[i]["transform_args"]
    >>>     )
    >>>     item[i]["signal"].widget(i)
    >>>
    >>> mix = sum([item[i]["signal"] for i in range(len(loaders))])
    >>> mix = dataset.transform(mix, **item["transform_args"])
    >>> mix.widget("mix")

    Below is an example of how one could load MUSDB multitrack data:

    >>> import audiotools as at
    >>> from pathlib import Path
    >>> from audiotools import transforms as tfm
    >>> import numpy as np
    >>> import torch
    >>>
    >>> def build_dataset(
    >>>     sample_rate: int = 44100,
    >>>     duration: float = 5.0,
    >>>     musdb_path: str = "~/.data/musdb/",
    >>> ):
    >>>     musdb_path = Path(musdb_path).expanduser()
    >>>     loaders = {
    >>>         src: at.datasets.AudioLoader(
    >>>             sources=[musdb_path],
    >>>             transform=tfm.Compose(
    >>>                 tfm.VolumeNorm(("uniform", -20, -10)),
    >>>                 tfm.Silence(prob=0.1),
    >>>             ),
    >>>             ext=[f"{src}.wav"],
    >>>         )
    >>>         for src in ["vocals", "bass", "drums", "other"]
    >>>     }
    >>>
    >>>     dataset = at.datasets.AudioDataset(
    >>>         loaders=loaders,
    >>>         sample_rate=sample_rate,
    >>>         duration=duration,
    >>>         num_channels=1,
    >>>         aligned=True,
    >>>         transform=tfm.RescaleAudio(),
    >>>         shuffle_loaders=True,
    >>>     )
    >>>     return dataset, list(loaders.keys())
    >>>
    >>> train_data, sources = build_dataset()
    >>> dataloader = torch.utils.data.DataLoader(
    >>>     train_data,
    >>>     batch_size=16,
    >>>     num_workers=0,
    >>>     collate_fn=train_data.collate,
    >>> )
    >>> batch = next(iter(dataloader))
    >>>
    >>> for k in sources:
    >>>     src = batch[k]
    >>>     src["transformed"] = train_data.loaders[k].transform(
    >>>         src["signal"].clone(), **src["transform_args"]
    >>>     )
    >>>
    >>> mixture = sum(batch[k]["transformed"] for k in sources)
    >>> mixture = train_data.transform(mixture, **batch["transform_args"])
    >>>
    >>> # Say a model takes the mix and gives back (n_batch, n_src, n_time).
    >>> # Construct the targets:
    >>> targets = at.AudioSignal.batch([batch[k]["transformed"] for k in sources], dim=1)

    Similarly, here's example code for loading Slakh data:

    >>> import audiotools as at
    >>> from pathlib import Path
    >>> from audiotools import transforms as tfm
    >>> import numpy as np
    >>> import torch
    >>> import glob
    >>>
    >>> def build_dataset(
    >>>     sample_rate: int = 16000,
    >>>     duration: float = 10.0,
    >>>     slakh_path: str = "~/.data/slakh/",
    >>> ):
    >>>     slakh_path = Path(slakh_path).expanduser()
    >>>
    >>>     # Find the max number of sources in Slakh
    >>>     src_names = [x.name for x in list(slakh_path.glob("**/*.wav"))  if "S" in str(x.name)]
    >>>     n_sources = len(list(set(src_names)))
    >>>
    >>>     loaders = {
    >>>         f"S{i:02d}": at.datasets.AudioLoader(
    >>>             sources=[slakh_path],
    >>>             transform=tfm.Compose(
    >>>                 tfm.VolumeNorm(("uniform", -20, -10)),
    >>>                 tfm.Silence(prob=0.1),
    >>>             ),
    >>>             ext=[f"S{i:02d}.wav"],
    >>>         )
    >>>         for i in range(n_sources)
    >>>     }
    >>>     dataset = at.datasets.AudioDataset(
    >>>         loaders=loaders,
    >>>         sample_rate=sample_rate,
    >>>         duration=duration,
    >>>         num_channels=1,
    >>>         aligned=True,
    >>>         transform=tfm.RescaleAudio(),
    >>>         shuffle_loaders=False,
    >>>     )
    >>>
    >>>     return dataset, list(loaders.keys())
    >>>
    >>> train_data, sources = build_dataset()
    >>> dataloader = torch.utils.data.DataLoader(
    >>>     train_data,
    >>>     batch_size=16,
    >>>     num_workers=0,
    >>>     collate_fn=train_data.collate,
    >>> )
    >>> batch = next(iter(dataloader))
    >>>
    >>> for k in sources:
    >>>     src = batch[k]
    >>>     src["transformed"] = train_data.loaders[k].transform(
    >>>         src["signal"].clone(), **src["transform_args"]
    >>>     )
    >>>
    >>> mixture = sum(batch[k]["transformed"] for k in sources)
    >>> mixture = train_data.transform(mixture, **batch["transform_args"])

    """

    def __init__(
        self,
        loaders: Union[AudioLoader, List[AudioLoader], Dict[str, AudioLoader]],
        sample_rate: int,
        n_examples: int = 1000,
        duration: float = 0.5,
        offset: float = None,
        loudness_cutoff: float = -40,
        num_channels: int = 1,
        transform: Callable = None,
        aligned: bool = False,
        shuffle_loaders: bool = False,
        matcher: Callable = default_matcher,
        without_replacement: bool = True,
    ):
        # Internally we convert loaders to a dictionary
        if isinstance(loaders, list):
            loaders = {i: l for i, l in enumerate(loaders)}
        elif isinstance(loaders, AudioLoader):
            loaders = {0: loaders}

        self.loaders = loaders
        self.loudness_cutoff = loudness_cutoff
        self.num_channels = num_channels

        self.length = n_examples
        self.transform = transform
        self.sample_rate = sample_rate
        self.duration = duration
        self.offset = offset
        self.aligned = aligned
        self.shuffle_loaders = shuffle_loaders
        self.without_replacement = without_replacement

        if aligned:
            loaders_list = list(loaders.values())
            for i in range(len(loaders_list[0].audio_lists)):
                input_lists = [l.audio_lists[i] for l in loaders_list]
                # Alignment happens in-place
                align_lists(input_lists, matcher)

    def __getitem__(self, idx):
        state = util.random_state(idx)
        offset = None if self.offset is None else self.offset
        item = {}

        keys = list(self.loaders.keys())
        if self.shuffle_loaders:
            state.shuffle(keys)

        loader_kwargs = {
            "state": state,
            "sample_rate": self.sample_rate,
            "duration": self.duration,
            "loudness_cutoff": self.loudness_cutoff,
            "num_channels": self.num_channels,
            "global_idx": idx if self.without_replacement else None,
        }

        # Draw item from first loader
        loader = self.loaders[keys[0]]
        item[keys[0]] = loader(**loader_kwargs)

        for key in keys[1:]:
            loader = self.loaders[key]
            if self.aligned:
                # Path mapper takes the current loader + everything
                # returned by the first loader.
                offset = item[keys[0]]["signal"].metadata["offset"]
                loader_kwargs.update(
                    {
                        "offset": offset,
                        "source_idx": item[keys[0]]["source_idx"],
                        "item_idx": item[keys[0]]["item_idx"],
                    }
                )
            item[key] = loader(**loader_kwargs)

        # Sort dictionary back into original order
        keys = list(self.loaders.keys())
        item = {k: item[k] for k in keys}

        item["idx"] = idx
        if self.transform is not None:
            item["transform_args"] = self.transform.instantiate(
                state=state, signal=item[keys[0]]["signal"]
            )

        # If there's only one loader, pop it up
        # to the main dictionary, instead of keeping it
        # nested.
        if len(keys) == 1:
            item.update(item.pop(keys[0]))

        return item

    def __len__(self):
        return self.length

    @staticmethod
    def collate(list_of_dicts: Union[list, dict], n_splits: int = None):
        """Collates items drawn from this dataset. Uses
        :py:func:`audiotools.core.util.collate`.

        Parameters
        ----------
        list_of_dicts : typing.Union[list, dict]
            Data drawn from each item.
        n_splits : int
            Number of splits to make when creating the batches (split into
            sub-batches). Useful for things like gradient accumulation.

        Returns
        -------
        dict
            Dictionary of batched data.
        """
        return util.collate(list_of_dicts, n_splits=n_splits)


class ConcatDataset(AudioDataset):
    def __init__(self, datasets: list):
        self.datasets = datasets

    def __len__(self):
        return sum([len(d) for d in self.datasets])

    def __getitem__(self, idx):
        dataset = self.datasets[idx % len(self.datasets)]
        return dataset[idx // len(self.datasets)]


class ResumableDistributedSampler(DistributedSampler):  # pragma: no cover
    """Distributed sampler that can be resumed from a given start index."""

    def __init__(self, dataset, start_idx: int = None, **kwargs):
        super().__init__(dataset, **kwargs)
        # Start index, allows to resume an experiment at the index it was
        self.start_idx = start_idx // self.num_replicas if start_idx is not None else 0

    def __iter__(self):
        for i, idx in enumerate(super().__iter__()):
            if i >= self.start_idx:
                yield idx
        self.start_idx = 0  # set the index back to 0 so for the next epoch


class ResumableSequentialSampler(SequentialSampler):  # pragma: no cover
    """Sequential sampler that can be resumed from a given start index."""

    def __init__(self, dataset, start_idx: int = None, **kwargs):
        super().__init__(dataset, **kwargs)
        # Start index, allows to resume an experiment at the index it was
        self.start_idx = start_idx if start_idx is not None else 0

    def __iter__(self):
        for i, idx in enumerate(super().__iter__()):
            if i >= self.start_idx:
                yield idx
        self.start_idx = 0  # set the index back to 0 so for the next epoch


def log_and_continue(exn):
    print(f"Handling webdataset error ({repr(exn)}). Ignoring.")
    return True


def decode_json(key, value):
    if "json" in key:
        return json.loads(value)


def decode_audiosignal(
    data: List[Dict[str, Any]],
    offset=None,
    duration=None,
    state=None,
    loudness_cutoff=-40,
    num_channels=1,
    sample_rate=44100,
    num_excerpts=50,
    max_excerpts=None,
    random_mono_channel=False,
):
    assert offset is None
    for sample in data:
        found_key = False
        for key, value in sample.items():
            extension = "." + re.sub(r".*[.]", "", key)
            if extension in util.AUDIO_EXTENSIONS:
                found_key = True
                break

        if not found_key:
            print(f"Warning: Failed to find audio key in sample with keys {sample.keys()}.")
            continue

        filelike = io.BytesIO(value)
        try:
            signals = AudioSignal.salient_excerpts(
                filelike,
                duration=duration,
                state=state,
                loudness_cutoff=loudness_cutoff,
                num_excerpts=num_excerpts,
                max_excerpts=max_excerpts,
            )
        except (RuntimeError, soundfile.LibsndfileError, ValueError) as e:
            if (
                isinstance(e, soundfile.LibsndfileError)
                or "The size of tensor a (5) must match the size of tensor b (6) at non-singleton dimension 1"
                in str(e)
                or "is empty!" in str(e)
                or "array is too big" in str(e)
            ):
                print(f"Error loading audio. Value: {key} Skipping...")
                continue
            else:
                raise e

        if num_channels == 1:
            if random_mono_channel:
                signals = signals.to_rand_mono()
            else:
                signals = signals.to_mono()
        signals = signals.resample(sample_rate)

        if signals.duration < duration:
            signals = signals.zero_pad_to(int(duration * sample_rate))
        del sample[key]
        for signal in signals:
            yield {**sample, "signal": signal}


def combine_json(data: Dict[str, Any]):
    audio_key = "signal"
    try:
        json_key = [k for k in data.keys() if "json" in k][0]
    except IndexError:
        return None
    data["json"] = data.pop(json_key)
    for k, v in data["json"].items():
        data[audio_key].metadata[k] = v
    return {"signal": data[audio_key]}


def add_transform_args(data: Dict[str, Any], transform=None, state=None):
    data["transform_args"] = transform.instantiate(state, signal=data["signal"])
    return data

def custom_tarfile_samples(
    src: Iterable[Dict[str, Any]],
    handler: Callable[[Exception], bool] = wds.tariterators.reraise_exception,
    select_files: Optional[Callable[[str], bool]] = None,
    rename_files: Optional[Callable[[str], str]] = None,
) -> Iterable[Dict[str, Any]]:
    """Given a stream of tar files, yield samples.

    Args:
        src: stream of tar files
        handler: exception handler
        select_files: function that selects files to be included

    Returns:
        stream of samples
    """
    streams = wds.tariterators.url_opener(src, handler=handler)
    files = wds.tariterators.tar_file_expander(
        streams, handler=wds.handlers.warn_and_continue, select_files=select_files, rename_files=rename_files
    )
    samples = wds.tariterators.group_by_keys(files, handler=handler, keys=lambda path: path.rsplit(".", 1))
    return samples

custom_tarfile_to_samples = wds.filters.pipelinefilter(custom_tarfile_samples)

def run_transform(data: Dict[str, Any], transform=None):
    signal = transform(
        data["signal"].clone(), **data["transform_args"]
    )
    return {"signal": signal}

class CustomWebDataset(wds.WebDataset):
    def __init__(
        self,
        urls: Union[str, Sequence[str]],
        batch_size: Optional[int] = None,
        shuffle: Optional[int] = None,
        shuffle_initial: Optional[int] = 1_000,
        resampled: bool = True,  # use shardlists.ResampledShards
        duration: float = 5.0,
        loudness_cutoff: int = -40,
        num_channels: int = 1,
        sample_rate: int = 44100,
        state: Optional[np.random.RandomState] = None,
        transform: Optional[Callable] = None,
        n_examples: int = 10_000_000,
        num_excerpts: int = 50,
        max_excerpts: Optional[int] = None,
        random_mono_channel: bool = False,
        share_urls_between_workers: bool = False,
        run_transform_in_dataset: bool = False,
        **kwargs,
    ):
        if share_urls_between_workers:
            urls = wds.shardlists.SimpleShardList(urls)
        super().__init__(
            urls=urls,
            resampled=resampled,
            handler=log_and_continue,
            nodesplitter=wds.shardlists.split_by_node
            if not resampled
            else wds.shardlists.single_node_only,
            **kwargs,
        )
        for idx, stage in enumerate(self.pipeline):
            if isinstance(stage, wds.filters.FilterFunction):
                self.pipeline.pop(idx)
                break
        self.pipeline.append(custom_tarfile_to_samples(handler=log_and_continue))

        self.n_examples = n_examples
        self.collate = util.collate

        _decode_audiosignal = partial(
            decode_audiosignal,
            duration=duration,
            loudness_cutoff=loudness_cutoff,
            num_channels=num_channels,
            sample_rate=sample_rate,
            state=state,
            num_excerpts=num_excerpts,
            max_excerpts=max_excerpts,
            random_mono_channel=random_mono_channel,
        )
        self.decode(decode_json)
        self.compose(_decode_audiosignal)
        self.map(combine_json)

        if transform is not None:
            _add_transform_args = partial(
                add_transform_args, transform=transform, state=state
            )
            self.map(_add_transform_args)

        if shuffle is not None:
            self.shuffle(shuffle, initial=shuffle_initial)

        if transform is not None and run_transform_in_dataset:
            _run_transform = partial(run_transform, transform=transform)
            self.map(_run_transform)

        if batch_size is not None:
            self.batched(batch_size, collation_fn=self.collate, partial=False)

    def __len__(self):
        return self.n_examples


class CustomWebDataloader(wds.WebLoader):
    def __init__(
        self,
        dataset: CustomWebDataset,
        num_workers: int = 8,
        epoch_steps: Optional[int] = None,
        prefetch_factor: int = 2,
        **kwargs,
    ):
        self.dataset = dataset
        if epoch_steps:
            dataset = dataset.with_epoch(epoch_steps)

        super().__init__(
            dataset,
            num_workers=num_workers,
            shuffle=False,
            pin_memory=True,
            batch_size=None,
            prefetch_factor=prefetch_factor,
            **kwargs,
        )

    def __len__(self):
        return len(self.dataset)
