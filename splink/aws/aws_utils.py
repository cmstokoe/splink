import os
import boto3


class boto_utils:

    def __init__(self, boto3_session: boto3.session.Session, output_bucket: str):

        if not type(boto3_session) == boto3.session.Session:
            raise ValueError("Please enter a valid boto3 session object.")

        self.boto3_session = boto3_session
        self.bucket = output_bucket

        # specify some additional prefixes
        self.temp_database_name_prefix = "__splink__temp__"
        self.user_id, self.s3_output = self.get_user_id_and_table_dir()
        self.temp_db_name = self.get_database_name_from_userid(self.user_id)

    def get_user_id_and_table_dir(self):

        sts_client = self.boto3_session.client("sts")
        sts_resp = sts_client.get_caller_identity()
        out_path = os.path.join("s3://", self.bucket, "splink_warehouse")
        if out_path[-1] != "/":
            out_path += "/"

        return (sts_resp["UserId"], out_path)

    def get_database_name_from_userid(self):

        unique_db_name = self.user_id.split(":")[-1].split("-", 1)[-1].replace("-", "_")
        unique_db_name = self.temp_database_name_prefix + unique_db_name
        return unique_db_name