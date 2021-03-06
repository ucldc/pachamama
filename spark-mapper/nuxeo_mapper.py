import sys
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.dynamicframe import DynamicFrame
from awsglue.utils import getResolvedOptions
from awsglue.transforms import *
from pyspark.sql.functions import *
from pyspark.sql.types import *


def main(database, table):

    # Create a glue DynamicFrame
    original_dyf = glue_context.create_dynamic_frame.from_catalog(
        database=database, table_name=table)

    # convert to apache spark dataframe
    original_df = (original_dyf.toDF().distinct())

    # register a user defined function for use by spark
    # FIXME write this in scala and call from python.
    # starting a python process is expensive and it is moreover
    # very costly to serialize the data to python
    spark.udf.register("map_rights_codes_py", map_rights_codes, StringType())

    # select columns that are a straight mapping
    direct_fields = [
        'uid',
        'calisphere-id'
    ]
    subfields = [
        'ucldc_schema:publisher',
        'ucldc_schema:alternativetitle',
        'ucldc_schema:extent',
        'ucldc_schema:publisher',
        'ucldc_schema:temporalcoverage',
        'dc:title',
        'ucldc_schema:type',
        'ucldc_schema:source',
        'ucldc_schema:provenance',
        'ucldc_schema:physlocation',
        'ucldc_schema:rightsstartdate',
        'ucldc_schema:transcription'
    ]
    direct_fields = [f for f in direct_fields if f in original_df.columns]
    subfields = check_subfields(original_df, 'properties', subfields)
    direct_fields = direct_fields + subfields
    if direct_fields:
        transformed_df = original_df.select(direct_fields)

    # process columns that need more complex unpacking/mapping
    date_df = map_date(original_df)
    if date_df:
        joinexpression = transformed_df['uid'] == date_df['date_uid']
        transformed_df = transformed_df.join(date_df, joinexpression)

    rights_df = map_rights(original_df)
    joinexpression = transformed_df['uid'] == rights_df['rights_uid']
    transformed_df = transformed_df.join(rights_df, joinexpression)

    subject_df = map_subject(original_df)
    if subject_df:
        joinexpression = transformed_df['uid'] == subject_df['subject_uid']
        transformed_df = transformed_df.join(subject_df, joinexpression)

    # title needs to be repeatable
    # physdesc needs to be made into a struct
    # relatedresource
    transformed_df = transformed_df.withColumn('collection_id', lit(table))

    # convert to glue dynamic frame
    transformed_dyf = DynamicFrame.fromDF(
        transformed_df, glue_context, "transformed_dyf")

    # rename columns
    transformed_dyf = transformed_dyf.apply_mapping([
            ('uid', 'string', 'nuxeo_uid', 'string'),
            ('calisphere-id', 'string', 'calisphere-id', 'string'),
            ('ucldc_schema:publisher', 'array', 'publisher', 'array'),
            ('ucldc_schema:alternativetitle',
                'array', 'alternative_title', 'array'),
            ('ucldc_schema:extent', 'string', 'extent', 'string'),
            ('ucldc_schema:temporalcoverage', 'array', 'temporal', 'array'),
            ('dc:title', 'string', 'title', 'string'),
            ('ucldc_schema:type', 'string', 'type', 'string'),
            ('ucldc_schema:source', 'string', 'source', 'string'),
            ('ucldc_schema:provenance', 'array', 'provenance', 'array'),
            ('ucldc_schema:physlocation', 'string', 'location', 'string'),
            ('ucldc_schema:transcription',
                'string', 'transcription', 'string'),
            ('date_mapped', 'array', 'date', 'array'),
            ('rights_mapped', 'array', 'rights', 'array'),
            ('subject_mapped', 'array', 'subject', 'array'),
            ('collection_id', 'string', 'collection_id', 'string')
        ])

    # write transformed data to target
    path = f"s3://rikolti/mapped_metadata/"

    partition_keys = ["collection_id"]
    glue_context.write_dynamic_frame.from_options(
       frame=transformed_dyf,
       connection_type="s3",
       connection_options={"path": path, "partitionKeys": partition_keys},
       format="json")

    return True


def map_rights(dataframe):

    rights_df = (dataframe
        .select(
            col('uid'),
            col('properties.ucldc_schema:rightsstatus'),
            col('properties.ucldc_schema:rightsstatement')
        )
        .selectExpr(
            'uid',
            'map_rights_codes_py(`ucldc_schema:rightsstatus`)',
            '`ucldc_schema:rightsstatement`'
        )
        .select('uid', array(
            'map_rights_codes_py(ucldc_schema:rightsstatus)',
            'ucldc_schema:rightsstatement').alias('rights_mapped')
        )
        .withColumnRenamed('uid', 'rights_uid')
    )

    return rights_df


def map_rights_codes(rights_str):
    '''Map the "coded" values of the rights status to a nice one for display
       This should really be a scala function which we call from python
    '''
    decoded = rights_str
    if rights_str == 'copyrighted':
        decoded = 'Copyrighted'
    elif rights_str == 'publicdomain':
        decoded = 'Public Domain'
    elif rights_str == 'unknown':
        decoded = 'Copyright Unknown'
    return decoded


def map_contributor(dataframe):

    pass


def map_creator(dataframe):

    pass


def map_date(dataframe):
    date_df = (dataframe
        .select(
            col('uid'),
            col('properties.ucldc_schema:date')
        ).withColumn('date_struct', explode(col('ucldc_schema:date')))
    )
    if date_df.count() > 0:
        date_df = (date_df
            .select('uid', 'date_struct.date')
            .groupBy('uid')
            .agg(collect_set('date'))
            .withColumnRenamed('uid', 'date_uid')
            .withColumnRenamed('collect_set(date)', 'date_mapped')
        )
        return date_df

    return None


def get_dtype(df, col):
    return [dtype for name, dtype in df.dtypes if name == col][0]


def check_subfields(df, field, subfields):
    field_df = df.select(f'{field}.*')
    subfields = [f'{field}.{s}' for s in subfields if s in field_df.columns]
    return subfields


def check_array_subfields(df, field, subfields):
    field_df = df.select(field)
    subfields = [f'{field}.{s}'
                 for s in subfields if s in f'{field_df.schema}']
    return subfields


def map_subject(dataframe):
    subject_fields = check_subfields(
        dataframe,
        'properties',
        ['ucldc_schema:subjecttopic', 'ucldc_schema:subjectname']
    )
    subject_subfields = []
    if 'properties.ucldc_schema:subjecttopic' in subject_fields:
        subject_subfields += check_array_subfields(
            dataframe,
            'properties.ucldc_schema:subjecttopic',
            ['heading']
        )
    if 'properties.ucldc_schema:subjectname' in subject_fields:
        subject_subfields += check_array_subfields(
            dataframe,
            'properties.ucldc_schema:subjectname',
            ['name'])

    if subject_subfields:
        flattened_subfields = [f.split('.')[-1] for f in subject_subfields]

        subject_df = dataframe.select(subject_subfields + ['uid'])
        if len(flattened_subfields) == 2:
            subject_df = (subject_df.select(
                    col('uid').alias('subject_uid'),
                    array_union(*flattened_subfields).alias('subject_mapped')
                )
            )
        else:
            subject_df = (subject_df.select(
                col('uid').alias('subject_uid'),
                col(flattened_subfields[0]).alias('subject_mapped')))

        return subject_df

    return None


if __name__ == "__main__":

    args = getResolvedOptions(sys.argv, ['JOB_NAME', 'collection_id'])

    # Create a Glue context
    glue_context = GlueContext(SparkContext.getOrCreate())

    # SparkSession provided with GlueContext. Pass this around
    # at runtime rather than instantiating within every python class
    spark = glue_context.spark_session

    print(args['collection_id'])
    main("rikolti", args['collection_id'])
