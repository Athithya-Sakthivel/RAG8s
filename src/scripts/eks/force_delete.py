import boto3
import sys
import time
from botocore.config import Config
from botocore.exceptions import ClientError

session = boto3.session.Session()
sts = session.client("sts")
account_id = sts.get_caller_identity()["Account"]

ec2 = session.client("ec2")
s3_client = session.client("s3", config=Config(retries={"max_attempts": 10}))
s3 = session.resource("s3", config=Config(retries={"max_attempts": 10}))

VPC_NAME = "rag-vpc"
VPC_ID = None

BUCKETS = [
    f"rag-staging-data-{account_id}",
    f"rag-staging-qdrant-backups-{account_id}",
]

def ask(prompt: str, expected: str) -> bool:
    ans = input(f"{prompt} ").strip()
    return ans == expected

def ignore(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ClientError as e:
        error_code = e.response['Error'].get('Code', 'Unknown')
        error_message = e.response['Error'].get('Message', 'Unknown error')
        print(f"    [!] {error_code}: {error_message}")
    except Exception as e:
        print(f"    [!] {e}")

def chunked(items, size=1000):
    for i in range(0, len(items), size):
        yield items[i:i+size]

def resolve_vpc_id_by_name(name: str):
    try:
        resp = ec2.describe_vpcs(Filters=[{"Name": "tag:Name", "Values": [name]}])
        vpcs = resp.get("Vpcs", [])
        if not vpcs:
            return None
        if len(vpcs) > 1:
            print(f"[!] Multiple VPCs found with Name={name}; using the first one: {vpcs[0]['VpcId']}")
        return vpcs[0]["VpcId"]
    except ClientError as e:
        print(f"[!] Error resolving VPC: {e}")
        return None

def wait_for_nat_gone(vpc_id: str, timeout=600, interval=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ngws = ec2.describe_nat_gateways(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            ).get("NatGateways", [])
        except ClientError as e:
            print(f"    [!] Error describing NAT gateways: {e}")
            return
        
        active = [n for n in ngws if n.get("State") not in ("deleted", "failed")]
        if not active:
            return
        print(f"    [..] Waiting on {len(active)} NAT gateway(s) to finish deleting")
        time.sleep(interval)
    print("    [!] NAT gateway wait timed out; continuing")

def cleanup_vpc(vpc_id: str):
    print(f"\n=== VPC cleanup: {vpc_id} ===")

    # NAT Gateways
    try:
        ngws = ec2.describe_nat_gateways(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("NatGateways", [])
        print(f"[+] NAT Gateways: {len(ngws)}")
        for ngw in ngws:
            ngw_state = ngw.get("State")
            if ngw_state != "deleted":
                print(f"  - delete NAT {ngw['NatGatewayId']} (state: {ngw_state})")
                ignore(ec2.delete_nat_gateway, NatGatewayId=ngw["NatGatewayId"])
        if ngws:
            wait_for_nat_gone(vpc_id)
    except ClientError as e:
        print(f"    [!] Error handling NAT gateways: {e}")

    # Internet Gateways
    try:
        igws = ec2.describe_internet_gateways(
            Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
        ).get("InternetGateways", [])
        print(f"[+] Internet Gateways: {len(igws)}")
        for igw in igws:
            print(f"  - detach/delete IGW {igw['InternetGatewayId']}")
            ignore(ec2.detach_internet_gateway, InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc_id)
            ignore(ec2.delete_internet_gateway, InternetGatewayId=igw["InternetGatewayId"])
    except ClientError as e:
        print(f"    [!] Error handling internet gateways: {e}")

    # VPC Endpoints
    try:
        eps = ec2.describe_vpc_endpoints(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("VpcEndpoints", [])
        print(f"[+] VPC Endpoints: {len(eps)}")
        if eps:
            ep_ids = [e["VpcEndpointId"] for e in eps if e.get("State") != "deleted"]
            if ep_ids:
                print(f"  - delete endpoints {ep_ids}")
                ignore(ec2.delete_vpc_endpoints, VpcEndpointIds=ep_ids)
    except ClientError as e:
        print(f"    [!] Error handling VPC endpoints: {e}")

    # Security Groups
    try:
        sgs = ec2.describe_security_groups(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("SecurityGroups", [])
        print(f"[+] Security Groups: {len(sgs)}")
        for sg in sgs:
            if sg.get("GroupName") != "default":
                print(f"  - delete SG {sg['GroupId']}")
                ignore(ec2.delete_security_group, GroupId=sg["GroupId"])
    except ClientError as e:
        print(f"    [!] Error handling security groups: {e}")

    # Route Tables
    try:
        rts = ec2.describe_route_tables(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("RouteTables", [])
        print(f"[+] Route Tables: {len(rts)}")
        for rt in rts:
            main = any(a.get("Main") for a in rt.get("Associations", []))
            if main:
                continue
            for assoc in rt.get("Associations", []):
                assoc_id = assoc.get("RouteTableAssociationId")
                if assoc_id:
                    print(f"  - disassociate route table assoc {assoc_id}")
                    ignore(ec2.disassociate_route_table, AssociationId=assoc_id)
            print(f"  - delete route table {rt['RouteTableId']}")
            ignore(ec2.delete_route_table, RouteTableId=rt["RouteTableId"])
    except ClientError as e:
        print(f"    [!] Error handling route tables: {e}")

    # Network Interfaces
    try:
        enis = ec2.describe_network_interfaces(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("NetworkInterfaces", [])
        print(f"[+] Network Interfaces: {len(enis)}")
        for eni in enis:
            eni_id = eni["NetworkInterfaceId"]
            eni_status = eni.get("Status", "unknown")
            att = eni.get("Attachment")
            if att and att.get("Status") == "attached":
                print(f"  - detach ENI {eni_id}")
                ignore(ec2.detach_network_interface, AttachmentId=att["AttachmentId"], Force=True)
            if eni_status != "deleted":
                print(f"  - delete ENI {eni_id}")
                ignore(ec2.delete_network_interface, NetworkInterfaceId=eni_id)
    except ClientError as e:
        print(f"    [!] Error handling network interfaces: {e}")

    # Subnets
    try:
        subs = ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        ).get("Subnets", [])
        print(f"[+] Subnets: {len(subs)}")
        for s in subs:
            if s.get("State") != "deleted":
                print(f"  - delete subnet {s['SubnetId']}")
                ignore(ec2.delete_subnet, SubnetId=s["SubnetId"])
    except ClientError as e:
        print(f"    [!] Error handling subnets: {e}")

    # VPC
    try:
        print(f"[+] Delete VPC {vpc_id}")
        ignore(ec2.delete_vpc, VpcId=vpc_id)
    except ClientError as e:
        print(f"    [!] Error deleting VPC: {e}")

def bucket_exists(bucket_name: str) -> bool:
    """Check if a bucket exists without failing."""
    try:
        s3_client.head_bucket(Bucket=bucket_name)
        return True
    except ClientError as e:
        error_code = e.response['Error'].get('Code', '')
        if error_code == '404':
            return False
        # For other errors (e.g., permissions), assume it doesn't exist
        print(f"    [!] Cannot verify bucket existence: {error_code}")
        return False

def cleanup_bucket(bucket_name: str):
    print(f"\n=== Bucket cleanup: {bucket_name} ===")

    # Check if bucket exists first
    if not bucket_exists(bucket_name):
        print(f"[i] Bucket {bucket_name} does not exist, skipping")
        return

    bucket = s3.Bucket(bucket_name)

    # Clean up multipart uploads
    try:
        uploads = s3_client.list_multipart_uploads(Bucket=bucket_name).get("Uploads", [])
        print(f"[+] Multipart uploads: {len(uploads)}")
        for up in uploads:
            print(f"  - abort upload {up['UploadId']} for key {up['Key']}")
            ignore(s3_client.abort_multipart_upload, Bucket=bucket_name, Key=up["Key"], UploadId=up["UploadId"])
    except ClientError as e:
        error_code = e.response['Error'].get('Code', 'Unknown')
        if error_code == 'NoSuchBucket':
            print(f"[i] Bucket {bucket_name} no longer exists, skipping upload cleanup")
            return
        print(f"    [!] Error cleaning multipart uploads: {error_code}")

    # Delete all object versions and delete markers
    try:
        paginator = s3_client.get_paginator("list_object_versions")
        to_delete = []

        for page in paginator.paginate(Bucket=bucket_name):
            for v in page.get("Versions", []):
                to_delete.append({"Key": v["Key"], "VersionId": v["VersionId"]})
            for dm in page.get("DeleteMarkers", []):
                to_delete.append({"Key": dm["Key"], "VersionId": dm["VersionId"]})

        print(f"[+] Object versions/delete markers: {len(to_delete)}")
        for batch in chunked(to_delete, 1000):
            print(f"  - deleting batch of {len(batch)}")
            ignore(s3_client.delete_objects, Bucket=bucket_name, Delete={"Objects": batch, "Quiet": True})
    except ClientError as e:
        error_code = e.response['Error'].get('Code', 'Unknown')
        if error_code == 'NoSuchBucket':
            print(f"[i] Bucket {bucket_name} no longer exists, skipping object cleanup")
            return
        print(f"    [!] Error deleting objects: {error_code}")
        # Don't return here - still try to delete the bucket

    # Delete the bucket
    try:
        print(f"[+] Delete bucket {bucket_name}")
        bucket.delete()
        print(f"    [✓] Bucket {bucket_name} deleted successfully")
    except ClientError as e:
        error_code = e.response['Error'].get('Code', 'Unknown')
        if error_code == 'NoSuchBucket':
            print(f"    [i] Bucket {bucket_name} already deleted")
        else:
            print(f"    [!] Error deleting bucket: {error_code}")

def main():
    global VPC_ID
    
    print("[i] Checking resources...")
    
    VPC_ID = resolve_vpc_id_by_name(VPC_NAME)
    
    if not VPC_ID:
        print(f"[!] VPC with Name tag '{VPC_NAME}' not found")
        # Don't exit - allow S3 cleanup to proceed
        do_vpc = False
    else:
        print(f"[+] Found VPC: {VPC_NAME} -> {VPC_ID}")
        do_vpc = ask("Type VPC to delete the VPC, or anything else to skip:", "VPC")

    # Check bucket existence
    bucket_status = {}
    for b in BUCKETS:
        bucket_status[b] = bucket_exists(b)
    
    print("\nPlanned destructive actions:")
    if do_vpc and VPC_ID:
        print(f"  VPC:    {VPC_NAME} -> {VPC_ID}")
    else:
        print(f"  VPC:    {VPC_NAME} (not found or skipped)")
    
    for b in BUCKETS:
        status = "exists" if bucket_status[b] else "not found"
        print(f"  Bucket: {b} ({status})")

    if not do_vpc:
        do_s3 = ask("Type S3 to delete the buckets, or anything else to skip:", "S3")
    else:
        do_s3 = False  # Already asked for VPC, no need to ask for S3 separately

    if not do_vpc and not do_s3:
        print("[!] Nothing selected; exiting")
        sys.exit(0)

    if not ask("Final confirmation: type DELETE to proceed:", "DELETE"):
        print("[!] Confirmation failed; exiting")
        sys.exit(0)

    if do_vpc and VPC_ID:
        cleanup_vpc(VPC_ID)

    if do_s3:
        for bucket_name in BUCKETS:
            cleanup_bucket(bucket_name)

    print("\n[✓] Requested cleanup completed")
    print("[i] Note: Some resources may have been already deleted (idempotent execution)")

if __name__ == "__main__":
    main()