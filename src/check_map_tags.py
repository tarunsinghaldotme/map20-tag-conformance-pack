import json
import sys
import datetime
import boto3
import botocore

try:
    import liblogging
except ImportError:
    pass

##############
# Parameters #
##############

# Define the default resource to report to Config Rules
DEFAULT_RESOURCE_TYPE = "AWS::::Account"

# Set to True to get the lambda to assume the Role attached on the Config Service (useful for cross-account).
ASSUME_ROLE_MODE = False

# Other parameters (no change needed)
CONFIG_ROLE_TIMEOUT_SECONDS = 900

#############
# Main Code #
#############

SUPPORTED_RESOURCE_TYPES = [
    # Compute
    'AWS::EC2::Instance',
    'AWS::Lambda::Function',

    # Storage
    'AWS::EC2::Volume',
    'AWS::S3::Bucket',
    'AWS::Backup::BackupPlan',
    'AWS::Backup::BackupVault',
    'AWS::FSx::FileSystem',
    'AWS::EFS::FileSystem',

    # Network
    'AWS::ElasticLoadBalancing::LoadBalancer',
    'AWS::ElasticLoadBalancingV2::LoadBalancer',
    'AWS::ApiGatewayV2::Api',
    'AWS::ApiGateway::RestApi',
    'AWS::EC2::TransitGateway',
    'AWS::NetworkFirewall::Firewall',
    'AWS::Route53::HostedZone',
    'AWS::DirectConnect::Connection',
    'AWS::DirectConnect::VirtualInterface',
    'AWS::CloudFront::Distribution',
    
    # Database
    'AWS::RDS::DBCluster',
    'AWS::RDS::DBInstance',
    'AWS::DynamoDB::Table',
    'AWS::OpenSearchService::Domain',

    # Application Integration
    'AWS::SNS::Topic',
    'AWS::SQS::Queue',
    'AWS::StepFunctions::StateMachine',

    # Analytics
    'AWS::QuickSight::Dashboard',
    'AWS::QuickSight::Analysis',
    'AWS::QuickSight::DataSet',

    # Migration & Transfer
    'AWS::DMS::ReplicationInstance',
    'AWS::DMS::ReplicationTask',
    'AWS::DataSync::Task',

    # Management & Governance
    'AWS::Logs::LogGroup',
    
    # Security, Identity, & Compliance
    'AWS::ACM::Certificate'

]

def evaluate_compliance(event, configuration_item, valid_rule_parameters):
    """Form the evaluation(s) to be return to Config Rules

    Return either:
    None -- when no result needs to be displayed
    a string -- either COMPLIANT, NON_COMPLIANT or NOT_APPLICABLE
    a dictionary -- the evaluation dictionary, usually built by build_evaluation_from_config_item()
    a list of dictionary -- a list of evaluation dictionary , usually built by build_evaluation()

    Keyword arguments:
    event -- the event variable given in the lambda handler
    configuration_item -- the configurationItem dictionary in the invokingEvent
    valid_rule_parameters -- the output of the evaluate_parameters() representing validated parameters of the Config Rule

    Advanced Notes:
    1 -- if a resource is deleted and generate a configuration change with ResourceDeleted status, the Boilerplate code will put a NOT_APPLICABLE on this resource automatically.
    2 -- if a None or a list of dictionary is returned, the old evaluation(s) which are not returned in the new evaluation list are returned as NOT_APPLICABLE by the Boilerplate code
    3 -- if None or an empty string, list or dict is returned, the Boilerplate code will put a "shadow" evaluation to feedback that the evaluation took place properly
    """

    # the rule is NOT_APPLICABLE if: the resource does not qualify for MAP 2.0 credits or is created before the MAP signing date.
    # the rule is COMPLIANT if: the resource qualifies for MAP 2.0 credits and is created before the MAP signing date. AND is tagged with map-migrated tag with the correct value
    # the rule is NOT_COMPLIANT otherwise.

    # If the resource does not qualify for MAP 2.0, then its NOT_APPLICABLE
    if configuration_item['resourceType'] not in SUPPORTED_RESOURCE_TYPES:
        return 'NOT_APPLICABLE'

    print('resource type: ',configuration_item['resourceType'])
    # print(configuration_item)

    # Some resources don't have a resourceCreationTime.
    if configuration_item['resourceCreationTime'] is None:
        return build_evaluation_from_config_item(configuration_item,'NON_COMPLIANT','resource has no creation date')

    # If the resrouce was created before the signing date, then its NOT_APPLICABLE
    resourceCreationDate = datetime.datetime.strptime(configuration_item['resourceCreationTime'],'%Y-%m-%dT%H:%M:%S.%fZ')
    # print(resourceCreationDate)
    # print(valid_rule_parameters['mapSignDate'])
    
    if resourceCreationDate < valid_rule_parameters['mapSignDate']:
        # print('NOT_APPLICABLE')
        return build_evaluation_from_config_item(configuration_item,'NOT_APPLICABLE','resource created before MAP signing date')

    # Check if any tags are defined.
    if 'tags' not in configuration_item:
        # print('NON_COMPLIANT: no tags defined')
        return build_evaluation_from_config_item(configuration_item,'NON_COMPLIANT','map-migrated tag not defined')

    # Check if map-migrated tag is defined
    if 'map-migrated' not in configuration_item['tags']:
        # print('NON_COMPLIANT: no map-migrated tag')
        return build_evaluation_from_config_item(configuration_item,'NON_COMPLIANT','map-migrated tag not defined')

    # Check if the map-migrated tag has the correct value
    if configuration_item['tags']['map-migrated'] == valid_rule_parameters['tagValue']:
        return 'COMPLIANT'
    else:
        # print('NON_COMPLIANT: in tag value')
        return build_evaluation_from_config_item(configuration_item,'NON_COMPLIANT','map-migrated tag value is invalid')


def evaluate_parameters(rule_parameters):
    """Evaluate the rule parameters dictionary validity. Raise a ValueError for invalid parameters.

    Return:
    anything suitable for the evaluate_compliance()

    Keyword arguments:
    rule_parameters -- the Key/Value dictionary of the Config Rules parameters
    """
    valid_rule_parameters = rule_parameters
    valid_rule_parameters['mapSignDate'] = datetime.datetime.strptime(rule_parameters['mapSignDate'],'%Y-%m-%d')
    return valid_rule_parameters


####################
# Helper Functions #
####################

# Build an error to be displayed in the logs when the parameter is invalid.
def build_parameters_value_error_response(ex):
    """Return an error dictionary when the evaluate_parameters() raises a ValueError.

    Keyword arguments:
    ex -- Exception text
    """
    return build_error_response(
        internal_error_message="Parameter value is invalid",
        internal_error_details="An ValueError was raised during the validation of the Parameter value",
        customer_error_code="InvalidParameterValueException",
        customer_error_message=str(ex),
    )


# This gets the client after assuming the Config service role
# either in the same AWS account or cross-account.
def get_client(service, event, region=None):
    """Return the service boto client. It should be used instead of directly calling the client.

    Keyword arguments:
    service -- the service name used for calling the boto.client()
    event -- the event variable given in the lambda handler
    region -- the region where the client is called (default: None)
    """
    if not ASSUME_ROLE_MODE:
        return boto3.client(service, region)
    credentials = get_assume_role_credentials(get_execution_role_arn(event), region)
    return boto3.client(
        service,
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region,
    )


# This generate an evaluation for config
def build_evaluation(resource_id, compliance_type, event, resource_type=DEFAULT_RESOURCE_TYPE, annotation=None):
    """Form an evaluation as a dictionary. Usually suited to report on scheduled rules.

    Keyword arguments:
    resource_id -- the unique id of the resource to report
    compliance_type -- either COMPLIANT, NON_COMPLIANT or NOT_APPLICABLE
    event -- the event variable given in the lambda handler
    resource_type -- the CloudFormation resource type (or AWS::::Account) to report on the rule (default DEFAULT_RESOURCE_TYPE)
    annotation -- an annotation to be added to the evaluation (default None). It will be truncated to 255 if longer.
    """
    eval_cc = {}
    if annotation:
        eval_cc["Annotation"] = build_annotation(annotation)
    eval_cc["ComplianceResourceType"] = resource_type
    eval_cc["ComplianceResourceId"] = resource_id
    eval_cc["ComplianceType"] = compliance_type
    eval_cc["OrderingTimestamp"] = str(json.loads(event["invokingEvent"])["notificationCreationTime"])
    return eval_cc


def build_evaluation_from_config_item(configuration_item, compliance_type, annotation=None):
    """Form an evaluation as a dictionary. Usually suited to report on configuration change rules.

    Keyword arguments:
    configuration_item -- the configurationItem dictionary in the invokingEvent
    compliance_type -- either COMPLIANT, NON_COMPLIANT or NOT_APPLICABLE
    annotation -- an annotation to be added to the evaluation (default None). It will be truncated to 255 if longer.
    """
    eval_ci = {}
    if annotation:
        eval_ci["Annotation"] = build_annotation(annotation)
    eval_ci["ComplianceResourceType"] = configuration_item["resourceType"]
    eval_ci["ComplianceResourceId"] = configuration_item["resourceId"]
    eval_ci["ComplianceType"] = compliance_type
    eval_ci["OrderingTimestamp"] = configuration_item["configurationItemCaptureTime"]
    return eval_ci


####################
# Boilerplate Code #
####################

# Get execution role for Lambda function
def get_execution_role_arn(event):
    role_arn = None
    if "ruleParameters" in event:
        rule_params = json.loads(event["ruleParameters"])
        role_name = rule_params.get("ExecutionRoleName")
        if role_name:
            execution_role_prefix = event["executionRoleArn"].split("/")[0]
            role_arn = "{}/{}".format(execution_role_prefix, role_name)

    if not role_arn:
        role_arn = event["executionRoleArn"]

    return role_arn


# Build annotation within Service constraints
def build_annotation(annotation_string):
    if len(annotation_string) > 256:
        return annotation_string[:244] + " [truncated]"
    return annotation_string


# Helper function used to validate input
def check_defined(reference, reference_name):
    if not reference:
        raise Exception("Error: ", reference_name, "is not defined")
    return reference


# Check whether the message is OversizedConfigurationItemChangeNotification or not
def is_oversized_changed_notification(message_type):
    check_defined(message_type, "messageType")
    return message_type == "OversizedConfigurationItemChangeNotification"


# Check whether the message is a ScheduledNotification or not.
def is_scheduled_notification(message_type):
    check_defined(message_type, "messageType")
    return message_type == "ScheduledNotification"


# Get configurationItem using getResourceConfigHistory API
# in case of OversizedConfigurationItemChangeNotification
def get_configuration(resource_type, resource_id, configuration_capture_time):
    result = AWS_CONFIG_CLIENT.get_resource_config_history(
        resourceType=resource_type, resourceId=resource_id, laterTime=configuration_capture_time, limit=1
    )
    configuration_item = result["configurationItems"][0]
    return convert_api_configuration(configuration_item)


# Convert from the API model to the original invocation model
def convert_api_configuration(configuration_item):
    for k, v in configuration_item.items():
        if isinstance(v, datetime.datetime):
            configuration_item[k] = str(v)
    configuration_item["awsAccountId"] = configuration_item["accountId"]
    configuration_item["ARN"] = configuration_item["arn"]
    configuration_item["configurationStateMd5Hash"] = configuration_item["configurationItemMD5Hash"]
    configuration_item["configurationItemVersion"] = configuration_item["version"]
    configuration_item["configuration"] = json.loads(configuration_item["configuration"])
    if "relationships" in configuration_item:
        for i in range(len(configuration_item["relationships"])):
            configuration_item["relationships"][i]["name"] = configuration_item["relationships"][i]["relationshipName"]
    return configuration_item


# Based on the type of message get the configuration item
# either from configurationItem in the invoking event
# or using the getResourceConfigHistiry API in getConfiguration function.
def get_configuration_item(invoking_event):
    check_defined(invoking_event, "invokingEvent")
    if is_oversized_changed_notification(invoking_event["messageType"]):
        configuration_item_summary = check_defined(
            invoking_event["configurationItemSummary"], "configurationItemSummary"
        )
        return get_configuration(
            configuration_item_summary["resourceType"],
            configuration_item_summary["resourceId"],
            configuration_item_summary["configurationItemCaptureTime"],
        )
    if is_scheduled_notification(invoking_event["messageType"]):
        return None
    return check_defined(invoking_event["configurationItem"], "configurationItem")


# Check whether the resource has been deleted. If it has, then the evaluation is unnecessary.
def is_applicable(configuration_item, event):
    try:
        check_defined(configuration_item, "configurationItem")
        check_defined(event, "event")
    except:
        return True
    status = configuration_item["configurationItemStatus"]
    event_left_scope = event["eventLeftScope"]
    if status == "ResourceDeleted":
        print("Resource Deleted, setting Compliance Status to NOT_APPLICABLE.")

    return status in ("OK", "ResourceDiscovered") and not event_left_scope


def get_assume_role_credentials(role_arn, region=None):
    sts_client = boto3.client("sts", region)
    try:
        assume_role_response = sts_client.assume_role(
            RoleArn=role_arn, RoleSessionName="configLambdaExecution", DurationSeconds=CONFIG_ROLE_TIMEOUT_SECONDS
        )
        if "liblogging" in sys.modules:
            liblogging.logSession(role_arn, assume_role_response)
        return assume_role_response["Credentials"]
    except botocore.exceptions.ClientError as ex:
        # Scrub error message for any internal account info leaks
        print(str(ex))
        if "AccessDenied" in ex.response["Error"]["Code"]:
            ex.response["Error"]["Message"] = "AWS Config does not have permission to assume the IAM role."
        else:
            ex.response["Error"]["Message"] = "InternalError"
            ex.response["Error"]["Code"] = "InternalError"
        raise ex


# This removes older evaluation (usually useful for periodic rule not reporting on AWS::::Account).
def clean_up_old_evaluations(latest_evaluations, event):

    cleaned_evaluations = []

    old_eval = AWS_CONFIG_CLIENT.get_compliance_details_by_config_rule(
        ConfigRuleName=event["configRuleName"], ComplianceTypes=["COMPLIANT", "NON_COMPLIANT"], Limit=100
    )

    old_eval_list = []

    while True:
        for old_result in old_eval["EvaluationResults"]:
            old_eval_list.append(old_result)
        if "NextToken" in old_eval:
            next_token = old_eval["NextToken"]
            old_eval = AWS_CONFIG_CLIENT.get_compliance_details_by_config_rule(
                ConfigRuleName=event["configRuleName"],
                ComplianceTypes=["COMPLIANT", "NON_COMPLIANT"],
                Limit=100,
                NextToken=next_token,
            )
        else:
            break

    for old_eval in old_eval_list:
        old_resource_id = old_eval["EvaluationResultIdentifier"]["EvaluationResultQualifier"]["ResourceId"]
        newer_founded = False
        for latest_eval in latest_evaluations:
            if old_resource_id == latest_eval["ComplianceResourceId"]:
                newer_founded = True
        if not newer_founded:
            cleaned_evaluations.append(build_evaluation(old_resource_id, "NOT_APPLICABLE", event))

    return cleaned_evaluations + latest_evaluations


def lambda_handler(event, context):
    if "liblogging" in sys.modules:
        liblogging.logEvent(event)

    global AWS_CONFIG_CLIENT

    # print(event)
    check_defined(event, "event")
    invoking_event = json.loads(event["invokingEvent"])
    rule_parameters = {}
    if "ruleParameters" in event:
        rule_parameters = json.loads(event["ruleParameters"])

    try:
        valid_rule_parameters = evaluate_parameters(rule_parameters)
    except ValueError as ex:
        return build_parameters_value_error_response(ex)

    try:
        AWS_CONFIG_CLIENT = get_client("config", event)
        if invoking_event["messageType"] in [
            "ConfigurationItemChangeNotification",
            "ScheduledNotification",
            "OversizedConfigurationItemChangeNotification",
        ]:
            configuration_item = get_configuration_item(invoking_event)
            if is_applicable(configuration_item, event):
                compliance_result = evaluate_compliance(event, configuration_item, valid_rule_parameters)
            else:
                compliance_result = "NOT_APPLICABLE"
        else:
            return build_internal_error_response("Unexpected message type", str(invoking_event))
    except botocore.exceptions.ClientError as ex:
        if is_internal_error(ex):
            return build_internal_error_response("Unexpected error while completing API request", str(ex))
        return build_error_response(
            "Customer error while making API request",
            str(ex),
            ex.response["Error"]["Code"],
            ex.response["Error"]["Message"],
        )
    except ValueError as ex:
        return build_internal_error_response(str(ex), str(ex))

    evaluations = []
    latest_evaluations = []

    if not compliance_result:
        latest_evaluations.append(
            build_evaluation(event["accountId"], "NOT_APPLICABLE", event, resource_type="AWS::::Account")
        )
        evaluations = clean_up_old_evaluations(latest_evaluations, event)
    elif isinstance(compliance_result, str):
        if configuration_item:
            evaluations.append(build_evaluation_from_config_item(configuration_item, compliance_result))
        else:
            evaluations.append(
                build_evaluation(event["accountId"], compliance_result, event, resource_type=DEFAULT_RESOURCE_TYPE)
            )
    elif isinstance(compliance_result, list):
        for evaluation in compliance_result:
            missing_fields = False
            for field in ("ComplianceResourceType", "ComplianceResourceId", "ComplianceType", "OrderingTimestamp"):
                if field not in evaluation:
                    print("Missing " + field + " from custom evaluation.")
                    missing_fields = True

            if not missing_fields:
                latest_evaluations.append(evaluation)
        evaluations = clean_up_old_evaluations(latest_evaluations, event)
    elif isinstance(compliance_result, dict):
        missing_fields = False
        for field in ("ComplianceResourceType", "ComplianceResourceId", "ComplianceType", "OrderingTimestamp"):
            if field not in compliance_result:
                print("Missing " + field + " from custom evaluation.")
                missing_fields = True
        if not missing_fields:
            evaluations.append(compliance_result)
    else:
        evaluations.append(build_evaluation_from_config_item(configuration_item, "NOT_APPLICABLE"))

    # Put together the request that reports the evaluation status
    result_token = event["resultToken"]
    test_mode = False

    # Invoke the Config API to report the result of the evaluation
    evaluation_copy = []
    evaluation_copy = evaluations[:]
    while evaluation_copy:
        AWS_CONFIG_CLIENT.put_evaluations(
            Evaluations=evaluation_copy[:100], ResultToken=result_token, TestMode=test_mode
        )
        del evaluation_copy[:100]

    # Used solely for RDK test to be able to test Lambda function
    return evaluations

def is_internal_error(exception):
    return (
        (not isinstance(exception, botocore.exceptions.ClientError))
        or exception.response["Error"]["Code"].startswith("5")
        or "InternalError" in exception.response["Error"]["Code"]
        or "ServiceError" in exception.response["Error"]["Code"]
    )

def build_internal_error_response(internal_error_message, internal_error_details=None):
    return build_error_response(internal_error_message, internal_error_details, "InternalError", "InternalError")

def build_error_response(
    internal_error_message, internal_error_details=None, customer_error_code=None, customer_error_message=None
):
    error_response = {
        "internalErrorMessage": internal_error_message,
        "internalErrorDetails": internal_error_details,
        "customerErrorMessage": customer_error_message,
        "customerErrorCode": customer_error_code,
    }
    print(error_response)
    return error_response
