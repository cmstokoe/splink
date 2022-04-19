import sqlglot
from splink.linker import Linker, SplinkDataFrame
import logging
from splink.logging_messages import execute_sql_logging_message_info, log_sql

# import utils for communicating with athena
import awswrangler as wr
import boto3
from splink.athena.athena_utils import boto_utils

logger = logging.getLogger(__name__)


class AthenaDataFrame(SplinkDataFrame):
    def __init__(self, templated_name, physical_name, athena_linker):
        super().__init__(templated_name, physical_name)
        self.athena_linker = athena_linker

    @property
    def columns(self):
        t = self.get_schema_info(self.physical_name)
        d = wr.catalog.get_table_types(database=t[0], table=t[1])

        return list(d.keys())

    def validate(self):
        pass

    def drop_table_from_database(self, force_non_splink_table=False):

        self._check_drop_folder_created_by_splink(force_non_splink_table)
        self._check_drop_table_created_by_splink(force_non_splink_table)
        self.athena_linker.drop_table_from_database_if_exists(self.physical_name)
        self.athena_linker.delete_table_from_s3(self.physical_name)

    def _check_drop_folder_created_by_splink(self, force_non_splink_table=False):

        filepath = self.athena_linker.boto_utils.s3_output
        filepath = filepath.split("/")[-3:-1]
        # validate that the write path is valid
        valid_path = [
            self.athena_linker.boto_utils.s3_output_name_prefix,
            self.athena_linker.boto_utils.session_id,
        ] == filepath
        if not valid_path:
            if not force_non_splink_table:
                raise ValueError(
                    f"You've asked to drop data housed under the filepath "
                    f"{self.athena_linker.boto_utils.s3_output} from your "
                    "s3 output bucket, which is not a folder created by "
                    "Splink. If you really want to delete this data, you "
                    "can do so by setting force_non_splink_table=True."
                )

        # validate that the ctas_query_info is for the given table
        # we're interacting with
        if (
            self.athena_linker.ctas_query_info[self.physical_name]["ctas_table"]
            != self.physical_name
        ):
            raise ValueError(
                f"The recorded metadata for {self.physical_name} that you're "
                "attempting to delete does not match the recorded metadata on s3. "
                "To prevent any tables becoming corrupted on s3, this run will be "
                "terminated. Please retry the link/dedupe job and report the issue "
                "if this error persists."
            )

    def as_record_dict(self, limit=None):
        sql = f"""
        select *
        from {self.physical_name}
        """
        if limit:
            sql += f" limit {limit}"

        out_df = wr.athena.read_sql_query(
            sql=sql,
            database=self.athena_linker.output_schema,
            s3_output=self.athena_linker.boto_utils.s3_output,
            keep_files=False,
        )
        return out_df.to_dict(orient="records")

    def get_schema_info(self, input_table):
        t = input_table.split(".")
        return (
            t if len(t) > 1 else [self.athena_linker.output_schema, self.physical_name]
        )


class AthenaLinker(Linker):
    def __init__(
        self,
        settings_dict: dict,
        boto3_session: boto3.session.Session,
        output_database: str,
        output_bucket: str,
        folder_in_bucket_for_outputs="",
        input_tables={},
    ):
        self.boto3_session = boto3_session
        self.boto_utils = boto_utils(
            boto3_session, output_bucket, folder_in_bucket_for_outputs
        )
        self.ctas_query_info = {}
        super().__init__(settings_dict, input_tables)
        self.output_schema = output_database

    def _df_as_obj(self, templated_name, physical_name):
        return AthenaDataFrame(templated_name, physical_name, self)

    def execute_sql(self, sql, templated_name, physical_name, transpile=True):

        # Deletes the table in the db, but not the object on s3.
        # This needs to be removed manually (full s3 path provided)
        self.drop_table_from_database_if_exists(physical_name)

        if transpile:
            sql = sqlglot.transpile(sql, read="spark", write="presto")[0]

        logger.debug(
            execute_sql_logging_message_info(
                templated_name, self._prepend_schema_to_table_name(physical_name)
            )
        )
        logger.log(5, log_sql(sql))

        # create our table on athena and extract the metadata information
        query_metadata = self.create_table(sql, physical_name=physical_name)
        # append our metadata locations
        self.ctas_query_info = {
            **self.ctas_query_info,
            **{physical_name: query_metadata},
        }

        output_obj = self._df_as_obj(templated_name, physical_name)
        return output_obj

    def random_sample_sql(self, proportion, sample_size):
        if proportion == 1.0:
            return ""
        percent = proportion * 100
        return f" TABLESAMPLE BERNOULLI ({percent})"

    def table_exists_in_database(self, table_name):
        rec = wr.catalog.does_table_exist(
            database=self.output_schema,
            table=table_name,
            boto3_session=self.boto3_session,
        )
        if not rec:
            return False
        else:
            return True

    def create_table(self, sql, physical_name):
        database = self.output_schema
        ctas_metadata = wr.athena.create_ctas_table(
            sql=sql,
            database=database,
            ctas_table=physical_name,
            ctas_database=database,
            storage_format="parquet",
            write_compression="snappy",
            boto3_session=self.boto3_session,
            s3_output=self.boto_utils.s3_output,
            wait=True,
        )
        return ctas_metadata

    def drop_table_from_database_if_exists(self, table):
        wr.catalog.delete_table_if_exists(
            database=self.output_schema, table=table, boto3_session=self.boto3_session
        )

    def delete_table_from_s3(self, physical_name):
        path = f"{self.boto_utils.s3_output}{physical_name}/"
        metadata = self.ctas_query_info[physical_name]
        metadata_urls = [
            # metadata output location
            f'{metadata["ctas_query_metadata"].output_location}.metadata',
            # manifest location
            metadata["ctas_query_metadata"].manifest_location,
        ]
        # delete our folder
        wr.s3.delete_objects(boto3_session=self.boto3_session, path=path)
        # delete our metadata
        wr.s3.delete_objects(boto3_session=self.boto3_session, path=metadata_urls)

        self.ctas_query_info.pop(physical_name)