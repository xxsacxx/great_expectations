import re
import datetime
import logging

try:
    import boto3
except ImportError:
    boto3 = None

from great_expectations.exceptions import GreatExpectationsError
from great_expectations.datasource.generator.batch_generator import BatchGenerator
from great_expectations.datasource.types import ReaderMethods, S3BatchKwargs
from great_expectations.exceptions import BatchKwargsError

logger = logging.getLogger(__name__)


class S3Generator(BatchGenerator):
    """
    S3 Generator provides support for generating batches of data from an S3 bucket. For the S3 generator, assets must
    be individually defined using a prefix and glob, although several additional configuration parameters are available
    for assets (see below).

    Example configuration::

        datasources:
          my_datasource:
            ...
            generators:
              my_s3_generator:
                class_name: S3Generator
                bucket: my_bucket.my_organization.priv
                reader_method: parquet  # This will be automatically inferred from suffix where possible, but can be explicitly specified as well
                reader_options:  # Note that reader options can be specified globally or per-asset
                  sep: ","
                delimiter: "/"  # Note that this is the delimiter for the BUCKET KEYS. By default it is "/"
                max_keys: 100  # The maximum number of keys to fetch in a single list_objects request to s3. When accessing batch_kwargs through an iterator, the iterator will silently refetch if more keys were available
                assets:
                  my_first_asset:
                    prefix: my_first_asset/
                    regex_filter: .*  # The regex filter will filter the results returned by S3 for the key and prefix to only those matching the regex
                    dictionary_assets: True
                  access_logs:
                    prefix: access_logs
                    regex_filter: access_logs/2019.*\.csv.gz
                    sep: "~"
                    max_keys: 100
    """

    # FIXME add tests for new partitioner functionality
    def __init__(self,
                 name="default",
                 datasource=None,
                 bucket=None,
                 reader_options=None,
                 assets=None,
                 delimiter="/",
                 reader_method=None,
                 max_keys=1000):
        """Initialize a new S3Generator

        Args:
            name: the name of the generator
            datasource: the datasource to which it is attached
            bucket: the name of the s3 bucket from which it generates batch_kwargs
            reader_options: options passed to the datasource reader method
            assets: asset configuration (see class docstring for more information)
            delimiter: the BUCKET KEY delimiter
            max_keys: the maximum number of keys to fetch in a single list_objects request to s3
        """
        super(S3Generator, self).__init__(name, datasource=datasource)
        if reader_options is None:
            reader_options = {}

        if assets is None:
            assets = {
                "default": {
                    "prefix": "",
                    "regex_filter": ".*"
                }
            }

        self._bucket = bucket
        self._reader_method = reader_method
        self._reader_options = reader_options
        self._assets = assets
        self._delimiter = delimiter
        self._max_keys = max_keys
        self._iterators = {}
        try:
            self._s3 = boto3.client('s3')
        except TypeError:
            raise(ImportError("Unable to load boto3, which is required for S3 generator"))

    @property
    def reader_options(self):
        return self._reader_options

    @property
    def assets(self):
        return self._assets

    @property
    def bucket(self):
        return self._bucket

    def get_available_data_asset_names(self):
        return self._assets.keys()

    def _get_iterator(self, generator_asset, reader_options=None, limit=None):
        logger.debug("Beginning S3Generator _get_iterator for generator_asset: %s" % generator_asset)

        if generator_asset not in self._assets:
            batch_kwargs = {
                "generator_asset": generator_asset,
                "reader_options": reader_options,
                "limit": limit
            }
            raise BatchKwargsError("Unknown asset_name %s" % generator_asset, batch_kwargs)

        if generator_asset not in self._iterators:
            self._iterators[generator_asset] = {}

        asset_config = self._assets[generator_asset]

        return self._build_asset_iterator(
            asset_config=asset_config,
            iterator_dict=self._iterators[generator_asset],
            reader_options=reader_options,
            limit=limit
        )

    def _build_batch_kwargs_path_iter(self, path_list, reader_options=None, limit=None):
        for path in path_list:
            yield self._build_batch_kwargs(path, reader_options=reader_options, limit=limit)

    def build_batch_kwargs_from_partition_id(self, generator_asset, partition_id=None, reader_options=None, limit=None):
        try:
            asset_config = self._assets[generator_asset]
        except KeyError:
            raise GreatExpectationsError(
                "No asset config found for asset %s" % generator_asset
            )
        if generator_asset not in self._iterators:
            self._iterators[generator_asset] = {}

        iterator_dict = self._iterators[generator_asset]
        batch_kwargs = None
        for key in self._get_asset_options(generator_asset, iterator_dict):
            if self._partitioner(key=key, asset_config=asset_config) == partition_id:
                batch_kwargs = self._build_batch_kwargs(key=key, asset_config=asset_config,
                                                        reader_options=reader_options, limit=limit)

        if batch_kwargs is None:
            raise BatchKwargsError(
                "Unable to identify partition %s for asset %s" % (partition_id, generator_asset),
               {
                    generator_asset: generator_asset,
                    partition_id: partition_id
                }
            )

        return batch_kwargs

    def _build_batch_kwargs(self, key, asset_config=None, reader_options=None, limit=None):
        batch_kwargs = {"s3": "s3a://" + self.bucket + "/" + key, 'reader_options': self.reader_options}
        if asset_config.get("reader_options"):
            batch_kwargs['reader_options'].update(asset_config.get("reader_options"))
        if reader_options is not None:
            batch_kwargs['reader_options'].update(reader_options)

        if self._reader_method is not None:
            batch_kwargs["reader_method"] = ReaderMethods(self._reader_method)
        if asset_config.get("reader_method"):
            batch_kwargs['reader_method'] = ReaderMethods(asset_config.get("reader_method"))

        if limit:
            batch_kwargs['limit'] = limit

        return S3BatchKwargs(batch_kwargs)

    def _get_asset_options(self, asset_config, iterator_dict):
        query_options = {
            "Bucket": self.bucket,
            "Delimiter": asset_config.get("delimiter", self._delimiter),
            "Prefix": asset_config.get("prefix", None),
            "MaxKeys": asset_config.get("max_keys", self._max_keys)
        }
        directory_assets = asset_config.get("directory_assets", False)

        if "continuation_token" in iterator_dict:
            query_options.update({
                "ContinuationToken": iterator_dict["continuation_token"]
            })

        logger.debug("Fetching objects from S3 with query options: %s" % str(query_options))
        asset_options = self._s3.list_objects_v2(**query_options)
        if directory_assets:
            if "CommonPrefixes" not in asset_options:
                raise BatchKwargsError(
                    "Unable to build batch_kwargs. The asset may not be configured correctly. If dictionary assets "
                    "are requested, then common prefixes must be returned.",
                    {
                        "asset_configuration": asset_config,
                        "contents": asset_options["Contents"] if "Contents" in asset_options else None
                    }
                )
            keys = [item["Prefix"] for item in asset_options["CommonPrefixes"]]
        else:
            if "Contents" not in asset_options:
                raise BatchKwargsError(
                    "Unable to build batch_kwargs. The asset may not be configured correctly. If s3 returned common "
                    "prefixes it may not have been able to identify desired keys, and they are included in the "
                    "incomplete batch_kwargs object returned with this error.",
                    {
                        "asset_configuration": asset_config,
                        "common_prefixes": asset_options["CommonPrefixes"]
                        if "CommonPrefixes" in asset_options else None
                    }
                )
            keys = [item["Key"] for item in asset_options["Contents"] if item["Size"] > 0]

        keys = [key for key in
                filter(lambda x: re.match(asset_config.get("regex_filter", ".*"), x) is not None, keys)]
        for key in keys:
            yield key

        if asset_options["IsTruncated"]:
            iterator_dict["continuation_token"] = asset_options["NextContinuationToken"]
            # Recursively fetch more
            for key in self._get_asset_options(asset_config, iterator_dict):
                yield key
        elif "continuation_token" in iterator_dict:
            # Make sure we clear the token once we've gotten fully through
            del iterator_dict["continuation_token"]

    def _build_asset_iterator(self, asset_config, iterator_dict, reader_options=None, limit=None):
        for key in self._get_asset_options(asset_config, iterator_dict):
            yield self._build_batch_kwargs(
                key,
                asset_config,
                reader_options=reader_options,
                limit=limit
            )

    def get_available_partition_ids(self, generator_asset):
        if generator_asset not in self._iterators:
            self._iterators[generator_asset] = {}
        iterator_dict = self._iterators[generator_asset]
        asset_config = self._assets[generator_asset]
        available_ids = [
            self._partitioner(key=key, asset_config=asset_config)
            for key in self._get_asset_options(generator_asset, iterator_dict)
        ]
        return available_ids

    def _partitioner(self, key, asset_config):
        if "partition_regex" in asset_config:
            match_group_id = asset_config.get("match_group_id", 1)
            matches = re.match(asset_config["partition_regex"], key)
            # In the case that there is a defined regex, the user *wanted* a partition. But it didn't match.
            # So, we'll add a *sortable* id
            if matches is None:
                logger.warning("No match found for key: %s" % key)
                return datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S.%fZ") + "__unmatched"
            else:
                try:
                    return matches.group(match_group_id)
                except IndexError:
                    logger.warning("No match group %d in key %s" % (match_group_id, key))
                    return datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S.%fZ") + "__no_match_group"

        # If there is no partitioner defined, fall back on using the path as a partition_id
        else:
            prefix = asset_config.get("prefix", "")
            if key.startswith(prefix):
                key = key[len(prefix):]
            return key

