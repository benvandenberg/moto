import json

from boto3 import Session

from moto.core.utils import iso_8601_datetime_without_milliseconds
from moto.core import BaseBackend, BaseModel
from moto.core.exceptions import RESTError
from moto.logs import logs_backends
from datetime import datetime, timedelta
from dateutil.tz import tzutc
from uuid import uuid4
from .utils import make_arn_for_dashboard
from dateutil import parser

from moto.core import ACCOUNT_ID as DEFAULT_ACCOUNT_ID

_EMPTY_LIST = tuple()


class Dimension(object):
    def __init__(self, name, value):
        self.name = name
        self.value = value

    def __eq__(self, item):
        if isinstance(item, Dimension):
            return self.name == item.name and self.value == item.value
        return False

    def __ne__(self, item):  # Only needed on Py2; Py3 defines it implicitly
        return self != item


def daterange(start, stop, step=timedelta(days=1), inclusive=False):
    """
    This method will iterate from `start` to `stop` datetimes with a timedelta step of `step`
    (supports iteration forwards or backwards in time)

    :param start: start datetime
    :param stop: end datetime
    :param step: step size as a timedelta
    :param inclusive: if True, last item returned will be as step closest to `end` (or `end` if no remainder).
    """

    # inclusive=False to behave like range by default
    total_step_secs = step.total_seconds()
    assert total_step_secs != 0

    if total_step_secs > 0:
        while start < stop:
            yield start
            start = start + step
    else:
        while stop < start:
            yield start
            start = start + step

    if inclusive and start == stop:
        yield start


class FakeAlarm(BaseModel):
    def __init__(
        self,
        name,
        namespace,
        metric_name,
        comparison_operator,
        evaluation_periods,
        period,
        threshold,
        statistic,
        description,
        dimensions,
        alarm_actions,
        ok_actions,
        insufficient_data_actions,
        unit,
        actions_enabled,
    ):
        self.name = name
        self.namespace = namespace
        self.metric_name = metric_name
        self.comparison_operator = comparison_operator
        self.evaluation_periods = evaluation_periods
        self.period = period
        self.threshold = threshold
        self.statistic = statistic
        self.description = description
        self.dimensions = [
            Dimension(dimension["name"], dimension["value"]) for dimension in dimensions
        ]
        self.actions_enabled = actions_enabled
        self.alarm_actions = alarm_actions
        self.ok_actions = ok_actions
        self.insufficient_data_actions = insufficient_data_actions
        self.unit = unit
        self.configuration_updated_timestamp = datetime.utcnow()

        self.history = []

        self.state_reason = ""
        self.state_reason_data = "{}"
        self.state_value = "OK"
        self.state_updated_timestamp = datetime.utcnow()

    def update_state(self, reason, reason_data, state_value):
        # History type, that then decides what the rest of the items are, can be one of ConfigurationUpdate | StateUpdate | Action
        self.history.append(
            (
                "StateUpdate",
                self.state_reason,
                self.state_reason_data,
                self.state_value,
                self.state_updated_timestamp,
            )
        )

        self.state_reason = reason
        self.state_reason_data = reason_data
        self.state_value = state_value
        self.state_updated_timestamp = datetime.utcnow()


class MetricDatum(BaseModel):
    def __init__(self, namespace, name, value, dimensions, timestamp):
        self.namespace = namespace
        self.name = name
        self.value = value
        self.timestamp = timestamp or datetime.utcnow().replace(tzinfo=tzutc())
        self.dimensions = [
            Dimension(dimension["Name"], dimension["Value"]) for dimension in dimensions
        ]

    def filter(self, namespace, name, dimensions):
        if namespace and namespace != self.namespace:
            return False
        if name and name != self.name:
            return False
        if dimensions and any(
            Dimension(d["Name"], d["Value"]) not in self.dimensions for d in dimensions
        ):
            return False
        return True


class Dashboard(BaseModel):
    def __init__(self, name, body):
        # Guaranteed to be unique for now as the name is also the key of a dictionary where they are stored
        self.arn = make_arn_for_dashboard(DEFAULT_ACCOUNT_ID, name)
        self.name = name
        self.body = body
        self.last_modified = datetime.now()

    @property
    def last_modified_iso(self):
        return self.last_modified.isoformat()

    @property
    def size(self):
        return len(self)

    def __len__(self):
        return len(self.body)

    def __repr__(self):
        return "<CloudWatchDashboard {0}>".format(self.name)


class Statistics:
    def __init__(self, stats, dt):
        self.timestamp = iso_8601_datetime_without_milliseconds(dt)
        self.values = []
        self.stats = stats

    @property
    def sample_count(self):
        if "SampleCount" not in self.stats:
            return None

        return len(self.values)

    @property
    def unit(self):
        return None

    @property
    def sum(self):
        if "Sum" not in self.stats:
            return None

        return sum(self.values)

    @property
    def minimum(self):
        if "Minimum" not in self.stats:
            return None

        return min(self.values)

    @property
    def maximum(self):
        if "Maximum" not in self.stats:
            return None

        return max(self.values)

    @property
    def average(self):
        if "Average" not in self.stats:
            return None

        # when moto is 3.4+ we can switch to the statistics module
        return sum(self.values) / len(self.values)


class CloudWatchBackend(BaseBackend):
    def __init__(self):
        self.alarms = {}
        self.dashboards = {}
        self.metric_data = []
        self.paged_metric_data = {}

    @property
    # Retrieve a list of all OOTB metrics that are provided by metrics providers
    # Computed on the fly
    def aws_metric_data(self):
        md = []
        for name, service in metric_providers.items():
            md.extend(service.get_cloudwatch_metrics())
        return md

    def put_metric_alarm(
        self,
        name,
        namespace,
        metric_name,
        comparison_operator,
        evaluation_periods,
        period,
        threshold,
        statistic,
        description,
        dimensions,
        alarm_actions,
        ok_actions,
        insufficient_data_actions,
        unit,
        actions_enabled,
    ):
        alarm = FakeAlarm(
            name,
            namespace,
            metric_name,
            comparison_operator,
            evaluation_periods,
            period,
            threshold,
            statistic,
            description,
            dimensions,
            alarm_actions,
            ok_actions,
            insufficient_data_actions,
            unit,
            actions_enabled,
        )
        self.alarms[name] = alarm
        return alarm

    def get_all_alarms(self):
        return self.alarms.values()

    @staticmethod
    def _list_element_starts_with(items, needle):
        """True of any of the list elements starts with needle"""
        for item in items:
            if item.startswith(needle):
                return True
        return False

    def get_alarms_by_action_prefix(self, action_prefix):
        return [
            alarm
            for alarm in self.alarms.values()
            if CloudWatchBackend._list_element_starts_with(
                alarm.alarm_actions, action_prefix
            )
        ]

    def get_alarms_by_alarm_name_prefix(self, name_prefix):
        return [
            alarm
            for alarm in self.alarms.values()
            if alarm.name.startswith(name_prefix)
        ]

    def get_alarms_by_alarm_names(self, alarm_names):
        return [alarm for alarm in self.alarms.values() if alarm.name in alarm_names]

    def get_alarms_by_state_value(self, target_state):
        return filter(
            lambda alarm: alarm.state_value == target_state, self.alarms.values()
        )

    def delete_alarms(self, alarm_names):
        for alarm_name in alarm_names:
            self.alarms.pop(alarm_name, None)

    def put_metric_data(self, namespace, metric_data):
        for metric_member in metric_data:
            # Preserve "datetime" for get_metric_statistics comparisons
            timestamp = metric_member.get("Timestamp")
            if timestamp is not None and type(timestamp) != datetime:
                timestamp = parser.parse(timestamp)
            self.metric_data.append(
                MetricDatum(
                    namespace,
                    metric_member["MetricName"],
                    float(metric_member.get("Value", 0)),
                    metric_member.get("Dimensions.member", _EMPTY_LIST),
                    timestamp,
                )
            )

    def get_metric_statistics(
        self, namespace, metric_name, start_time, end_time, period, stats
    ):
        period_delta = timedelta(seconds=period)
        filtered_data = [
            md
            for md in self.metric_data
            if md.namespace == namespace
            and md.name == metric_name
            and start_time <= md.timestamp <= end_time
        ]

        # earliest to oldest
        filtered_data = sorted(filtered_data, key=lambda x: x.timestamp)
        if not filtered_data:
            return []

        idx = 0
        data = list()
        for dt in daterange(
            filtered_data[0].timestamp,
            filtered_data[-1].timestamp + period_delta,
            period_delta,
        ):
            s = Statistics(stats, dt)
            while idx < len(filtered_data) and filtered_data[idx].timestamp < (
                dt + period_delta
            ):
                s.values.append(filtered_data[idx].value)
                idx += 1

            if not s.values:
                continue

            data.append(s)

        return data

    def get_all_metrics(self):
        return self.metric_data + self.aws_metric_data

    def put_dashboard(self, name, body):
        self.dashboards[name] = Dashboard(name, body)

    def list_dashboards(self, prefix=""):
        for key, value in self.dashboards.items():
            if key.startswith(prefix):
                yield value

    def delete_dashboards(self, dashboards):
        to_delete = set(dashboards)
        all_dashboards = set(self.dashboards.keys())

        left_over = to_delete - all_dashboards
        if len(left_over) > 0:
            # Some dashboards are not found
            return (
                False,
                "The specified dashboard does not exist. [{0}]".format(
                    ", ".join(left_over)
                ),
            )

        for dashboard in to_delete:
            del self.dashboards[dashboard]

        return True, None

    def get_dashboard(self, dashboard):
        return self.dashboards.get(dashboard)

    def set_alarm_state(self, alarm_name, reason, reason_data, state_value):
        try:
            if reason_data is not None:
                json.loads(reason_data)
        except ValueError:
            raise RESTError("InvalidFormat", "StateReasonData is invalid JSON")

        if alarm_name not in self.alarms:
            raise RESTError(
                "ResourceNotFound", "Alarm {0} not found".format(alarm_name), status=404
            )

        if state_value not in ("OK", "ALARM", "INSUFFICIENT_DATA"):
            raise RESTError(
                "InvalidParameterValue",
                "StateValue is not one of OK | ALARM | INSUFFICIENT_DATA",
            )

        self.alarms[alarm_name].update_state(reason, reason_data, state_value)

    def list_metrics(self, next_token, namespace, metric_name, dimensions):
        if next_token:
            if next_token not in self.paged_metric_data:
                raise RESTError(
                    "PaginationException", "Request parameter NextToken is invalid"
                )
            else:
                metrics = self.paged_metric_data[next_token]
                del self.paged_metric_data[next_token]  # Cant reuse same token twice
                return self._get_paginated(metrics)
        else:
            metrics = self.get_filtered_metrics(metric_name, namespace, dimensions)
            return self._get_paginated(metrics)

    def get_filtered_metrics(self, metric_name, namespace, dimensions):
        metrics = self.get_all_metrics()
        metrics = [
            md
            for md in metrics
            if md.filter(namespace=namespace, name=metric_name, dimensions=dimensions)
        ]
        return metrics

    def _get_paginated(self, metrics):
        if len(metrics) > 500:
            next_token = str(uuid4())
            self.paged_metric_data[next_token] = metrics[500:]
            return next_token, metrics[0:500]
        else:
            return None, metrics


class LogGroup(BaseModel):
    def __init__(self, spec):
        # required
        self.name = spec["LogGroupName"]
        # optional
        self.tags = spec.get("Tags", [])

    @classmethod
    def create_from_cloudformation_json(
        cls, resource_name, cloudformation_json, region_name
    ):
        properties = cloudformation_json["Properties"]
        log_group_name = properties["LogGroupName"]
        tags = properties.get("Tags", {})
        return logs_backends[region_name].create_log_group(
            log_group_name, tags, **properties
        )


cloudwatch_backends = {}
for region in Session().get_available_regions("cloudwatch"):
    cloudwatch_backends[region] = CloudWatchBackend()
for region in Session().get_available_regions(
    "cloudwatch", partition_name="aws-us-gov"
):
    cloudwatch_backends[region] = CloudWatchBackend()
for region in Session().get_available_regions("cloudwatch", partition_name="aws-cn"):
    cloudwatch_backends[region] = CloudWatchBackend()

# List of services that provide OOTB CW metrics
# See the S3Backend constructor for an example
# TODO: We might have to separate this out per region for non-global services
metric_providers = {}
