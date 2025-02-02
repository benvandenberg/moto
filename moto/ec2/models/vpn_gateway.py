from typing import Optional
from moto.core import CloudFormationModel
from .core import TaggedEC2Resource
from ..exceptions import InvalidVpnGatewayIdError, InvalidVpnGatewayAttachmentError
from ..utils import generic_filter, random_vpn_gateway_id


class VPCGatewayAttachment(CloudFormationModel):
    # Represents both VPNGatewayAttachment and VPCGatewayAttachment
    def __init__(
        self, vpc_id: str, gateway_id: Optional[str] = None, state: Optional[str] = None
    ):
        self.vpc_id = vpc_id
        self.gateway_id = gateway_id
        self.state = state

    @staticmethod
    def cloudformation_name_type():
        return None

    @staticmethod
    def cloudformation_type():
        # https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-ec2-vpcgatewayattachment.html
        return "AWS::EC2::VPCGatewayAttachment"

    @classmethod
    def create_from_cloudformation_json(
        cls, resource_name, cloudformation_json, account_id, region_name, **kwargs
    ):
        from ..models import ec2_backends

        properties = cloudformation_json["Properties"]

        ec2_backend = ec2_backends[account_id][region_name]
        vpn_gateway_id = properties.get("VpnGatewayId", None)
        internet_gateway_id = properties.get("InternetGatewayId", None)
        if vpn_gateway_id:
            attachment = ec2_backend.attach_vpn_gateway(
                vpc_id=properties["VpcId"], vpn_gateway_id=vpn_gateway_id
            )
        elif internet_gateway_id:
            attachment = ec2_backend.attach_internet_gateway(
                internet_gateway_id=internet_gateway_id, vpc_id=properties["VpcId"]
            )
        return attachment

    @property
    def physical_resource_id(self):
        return self.vpc_id


class VpnGateway(CloudFormationModel, TaggedEC2Resource):
    def __init__(
        self,
        ec2_backend,
        gateway_id,
        gateway_type,
        amazon_side_asn,
        availability_zone,
        tags=None,
        state="available",
    ):
        self.ec2_backend = ec2_backend
        self.id = gateway_id
        self.type = gateway_type
        self.amazon_side_asn = amazon_side_asn
        self.availability_zone = availability_zone
        self.state = state
        self.add_tags(tags or {})
        self.attachments = {}
        super().__init__()

    @staticmethod
    def cloudformation_name_type():
        return None

    @staticmethod
    def cloudformation_type():
        # https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-ec2-vpcgatewayattachment.html
        return "AWS::EC2::VPNGateway"

    @classmethod
    def create_from_cloudformation_json(
        cls, resource_name, cloudformation_json, account_id, region_name, **kwargs
    ):
        from ..models import ec2_backends

        properties = cloudformation_json["Properties"]
        _type = properties["Type"]
        asn = properties.get("AmazonSideAsn", None)
        ec2_backend = ec2_backends[account_id][region_name]

        return ec2_backend.create_vpn_gateway(gateway_type=_type, amazon_side_asn=asn)

    @property
    def physical_resource_id(self):
        return self.id

    def get_filter_value(self, filter_name):
        if filter_name == "attachment.vpc-id":
            return self.attachments.keys()
        elif filter_name == "attachment.state":
            return [attachment.state for attachment in self.attachments.values()]
        elif filter_name == "vpn-gateway-id":
            return self.id
        elif filter_name == "type":
            return self.type
        return super().get_filter_value(filter_name, "DescribeVpnGateways")


class VpnGatewayBackend:
    def __init__(self):
        self.vpn_gateways = {}

    def create_vpn_gateway(
        self,
        gateway_type="ipsec.1",
        amazon_side_asn=None,
        availability_zone=None,
        tags=None,
    ):
        vpn_gateway_id = random_vpn_gateway_id()
        vpn_gateway = VpnGateway(
            self, vpn_gateway_id, gateway_type, amazon_side_asn, availability_zone, tags
        )
        self.vpn_gateways[vpn_gateway_id] = vpn_gateway
        return vpn_gateway

    def describe_vpn_gateways(self, filters=None, vpn_gw_ids=None):
        vpn_gateways = list(self.vpn_gateways.values() or [])
        if vpn_gw_ids:
            vpn_gateways = [item for item in vpn_gateways if item.id in vpn_gw_ids]
        return generic_filter(filters, vpn_gateways)

    def get_vpn_gateway(self, vpn_gateway_id):
        vpn_gateway = self.vpn_gateways.get(vpn_gateway_id, None)
        if not vpn_gateway:
            raise InvalidVpnGatewayIdError(vpn_gateway_id)
        return vpn_gateway

    def attach_vpn_gateway(self, vpn_gateway_id, vpc_id):
        vpn_gateway = self.get_vpn_gateway(vpn_gateway_id)
        self.get_vpc(vpc_id)
        attachment = VPCGatewayAttachment(vpc_id, state="attached")
        for key in vpn_gateway.attachments.copy():
            if key.startswith("vpc-"):
                vpn_gateway.attachments.pop(key)
        vpn_gateway.attachments[vpc_id] = attachment
        return attachment

    def delete_vpn_gateway(self, vpn_gateway_id):
        deleted = self.vpn_gateways.get(vpn_gateway_id, None)
        if not deleted:
            raise InvalidVpnGatewayIdError(vpn_gateway_id)
        deleted.state = "deleted"
        return deleted

    def detach_vpn_gateway(self, vpn_gateway_id, vpc_id):
        vpn_gateway = self.get_vpn_gateway(vpn_gateway_id)
        detached = vpn_gateway.attachments.get(vpc_id, None)
        if not detached:
            raise InvalidVpnGatewayAttachmentError(vpn_gateway.id, vpc_id)
        detached.state = "detached"
        return detached
