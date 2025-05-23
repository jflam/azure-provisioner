"""Provider-specific quota adapters."""
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple
import importlib
import requests
from azure.identity import DefaultAzureCredential
from azure.core.exceptions import HttpResponseError
from .models import QuotaInfo, ResourceQuota

class ProviderAdapter(ABC):
    """Base class for provider-specific quota adapters."""
    
    def __init__(self, subscription_id: str):
        """Initialize the adapter.
        
        Args:
            subscription_id: Azure subscription ID.
        """
        self.subscription_id = subscription_id
        self.credential = DefaultAzureCredential()
    
    @abstractmethod
    def check_quota(self, resource_type: str, region: str, capacity: Dict) -> ResourceQuota:
        """Check quotas for a specific resource type in a region.
        
        Args:
            resource_type: Azure resource type (e.g., "Microsoft.Web/staticSites").
            region: Azure region name.
            capacity: Dictionary containing unit and required capacity.
            
        Returns:
            ResourceQuota: Quota information for the resource.
        """
        pass

class ComputeProviderAdapter(ProviderAdapter):
    """Adapter for Microsoft.Compute quota checks."""
    
    def check_quota(self, resource_type: str, region: str, capacity: Dict) -> ResourceQuota:
        from azure.mgmt.compute import ComputeManagementClient
        
        client = ComputeManagementClient(self.credential, self.subscription_id)
        usages = client.usage.list(region)
        
        result = ResourceQuota(resource_type, region, {})
        
        # Map capacity units to Azure usage names
        unit_mappings = {
            "vCores": "standardDSv3Family",
            # Add other mappings
        }
        
        for usage in usages:
            # Check if this usage matches what we need
            if usage.name.value.lower() == unit_mappings.get(capacity["unit"].lower()):
                quota_info = QuotaInfo(
                    unit=capacity["unit"],
                    current_usage=usage.current_value,
                    limit=usage.limit,
                    required=capacity["required"]
                )
                result.quotas[capacity["unit"]] = quota_info
                break
        
        return result

class WebProviderAdapter(ProviderAdapter):
    """Adapter for Microsoft.Web quota checks."""
    
    def check_quota(self, resource_type: str, region: str, capacity: Dict) -> ResourceQuota:
        from azure.mgmt.web import WebSiteManagementClient
        
        client = WebSiteManagementClient(self.credential, self.subscription_id)
        usages = client.usages.list_by_location(region)
        
        result = ResourceQuota(resource_type, region, {})
        
        for usage in usages:
            if usage.name.value.lower() == capacity["unit"].lower():
                quota_info = QuotaInfo(
                    unit=capacity["unit"],
                    current_usage=usage.current_value,
                    limit=usage.limit,
                    required=capacity["required"]
                )
                result.quotas[capacity["unit"]] = quota_info
                break
        
        return result

class PostgreSQLProviderAdapter(ProviderAdapter):
    """Adapter for Microsoft.DBforPostgreSQL quota checks."""
    
    def check_quota(self, resource_type: str, region: str, capacity: Dict) -> ResourceQuota:
        result = ResourceQuota(resource_type, region, {})
        
        try:
            token_response = self.credential.get_token("https://management.azure.com/.default")
            token = token_response.token
        except Exception as e:
            print(f"Error acquiring token for PostgreSQL quota check: {e}")
            return result

        api_version = "2024-11-01-preview"
        
        # Check if the resource_type is for flexibleServers
        if resource_type.lower() != "microsoft.dbforpostgresql/flexibleservers":
            print(f"Warning: PostgreSQLProviderAdapter is specialized for flexibleServers, received {resource_type}")
            return result

        url = (f"https://management.azure.com/subscriptions/{self.subscription_id}"
               f"/providers/Microsoft.DBforPostgreSQL/locations/{region}"
               f"/resourceType/flexibleServers/usages"
               f"?api-version={api_version}")

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            
            usages_data = response.json()
            found_quota = False
            
            # Map user-facing units to the strings Azure uses
            unit_mappings = {
                "vcores": {"cores"},  # total vCPU quota
                # add more mappings as needed
            }
            requested = capacity["unit"].lower()

            for item in usages_data.get("value", []):
                api_quota_name = (item.get("name", {}) or {}).get("value", "").lower()
                if api_quota_name and api_quota_name in unit_mappings.get(requested, {requested}):
                    limit = item.get("limit")
                    current_value = item.get("currentValue")

                    if limit is None or current_value is None:
                        print(f"Warning: Missing limit or currentValue for {api_quota_name} in {region} for PostgreSQL.")
                        continue

                    try:
                        limit_val = float(limit)
                        current_val = float(current_value)
                    except ValueError:
                        print(f"Warning: Non-numeric limit/currentValue for {api_quota_name} in {region} for PostgreSQL.")
                        continue

                    quota_info = QuotaInfo(
                        unit=capacity["unit"],
                        current_usage=current_val,
                        limit=limit_val,
                        required=float(capacity["required"])
                    )
                    result.quotas[capacity["unit"]] = quota_info
                    found_quota = True
                    break
            
            if not found_quota:
                print(f"Warning: Quota unit '{capacity['unit']}' not found for {resource_type} in {region}. Available: {[item.get('name', {}).get('value') for item in usages_data.get('value', [])]}")
                # If quota unit not found, mark as insufficient
                result.quotas[capacity["unit"]] = QuotaInfo(
                    unit=capacity["unit"],
                    current_usage=0,
                    limit=0,
                    required=float(capacity["required"])
                )

        except HttpResponseError as e:
            print(f"Error checking PostgreSQL quota via REST for {resource_type} in {region}: {e.response.status_code} - {e.response.text}")
        except requests.exceptions.RequestException as e:
            print(f"RequestException checking PostgreSQL quota for {resource_type} in {region}: {e}")
        except Exception as e:
            print(f"Unexpected error checking PostgreSQL quota for {resource_type} in {region}: {e}")
            
        return result

class ContainerAppsProviderAdapter(ProviderAdapter):
    """Adapter for Microsoft.App quota checks."""
    
    def check_quota(self, resource_type: str, region: str, capacity: Dict) -> ResourceQuota:
        from azure.mgmt.appcontainers import ContainerAppsAPIClient
        
        client = ContainerAppsAPIClient(self.credential, self.subscription_id)
        result = ResourceQuota(resource_type, region, {})
        found_quota = False
        
        # Check if we're looking for cores in a Container Apps environment
        requested_unit = capacity["unit"].lower()
        
        # For core quotas in Container Apps, check at the environment level
        if requested_unit == "cores" and resource_type == "Microsoft.App/managedEnvironments":
            env_name = capacity.get("environment_name")
            resource_group = capacity.get("resource_group")
            
            # If environment details are provided, query environment-level quotas
            if env_name and resource_group:
                try:
                    # First, check if the resource group and environment exist
                    # If not, we'll use the default quota values since we're just planning
                    try:
                        # The SDK returns an iterator; convert to list so that we can traverse it multiple times
                        # Method expects positional arguments: resource_group_name, environment_name  
                        env_usages = list(client.managed_environment_usages.list(
                            resource_group, 
                            env_name
                        ))
                        
                        # Environment-level quota mapping
                        unit_mappings = {
                            "cores": {
                                "managedenvironmentconsumptioncores",
                                "managedenvironmentgeneralpurposecores",
                                "managedenvironmentmemoryoptimizedcores",
                            },
                        }
                        
                        # Process environment-level usages
                        for usage in env_usages:
                            if usage.name and usage.name.value:
                                api_name = usage.name.value.lower()
                                # Match any core usage type in environment
                                if (
                                    api_name in unit_mappings.get(requested_unit, {requested_unit})
                                    or (
                                        requested_unit == "cores"
                                        and ("core" in api_name)
                                    )
                                ):
                                    limit = usage.limit
                                    current_value = usage.current_value

                                    if limit is None or current_value is None:
                                        print(f"Warning: Missing limit or currentValue for {usage.name.value} in environment {env_name}.")
                                        continue

                                    try:
                                        limit_val = float(limit)
                                        current_val = float(current_value)
                                    except ValueError:
                                        print(f"Warning: Non-numeric limit/currentValue for {usage.name.value} in environment {env_name}.")
                                        continue
                                    
                                    quota_info = QuotaInfo(
                                        unit=capacity["unit"],
                                        current_usage=current_val,
                                        limit=limit_val,
                                        required=float(capacity["required"])
                                    )
                                    result.quotas[capacity["unit"]] = quota_info
                                    found_quota = True
                                    break
                                    
                        if not found_quota:
                            available_units = [
                                u.name.value
                                for u in env_usages
                                if hasattr(u, "name") and u.name and hasattr(u.name, "value") and u.name.value
                            ]
                            print(
                                f"Warning: Quota unit '{capacity['unit']}' not found in environment {env_name}. "
                                f"Available: {available_units}"
                            )
                            # Use default quota values
                            raise Exception("No matching quota units found in environment")
                    
                    except Exception as rg_error:
                        # Environment or resource group doesn't exist yet or no matching quotas found
                        # Use the default 100 cores per environment limit as per Azure documentation
                        error_str = str(rg_error)
                        # Format error message with indentation for better readability
                        formatted_error = error_str.replace("\n", "\n    ")
                        print(f"Warning: Using default Container Apps environment quota limits (environment may not exist yet):\n    {formatted_error}")
                        quota_info = QuotaInfo(
                            unit=capacity["unit"],
                            current_usage=0,
                            limit=100.0,  # Default 100 cores per environment
                            required=float(capacity["required"])
                        )
                        result.quotas[capacity["unit"]] = quota_info
                        found_quota = True
                        
                except Exception as e:
                    print(f"Error checking Container Apps environment quota: {e}")
                    # Fall back to region-level check
            else:
                print(f"Warning: Container Apps core quota check requires environment_name and resource_group in capacity. Using region-level check.")
        
        # If we're not checking environment-level cores or the environment check failed,
        # fall back to region-level quota checking (for environment count, etc.)
        if not found_quota:
            # The SDK returns an iterator; convert to list
            usages = list(client.usages.list(location=region))
            
            # Map friendly units → exact names returned by the API.
            unit_mappings = {
                "cores": {
                    "managedenvironmentcores",
                    "managedenvironmentconsumptioncores",
                    "managedenvironmentgeneralpurposecores",
                    "managedenvironmentmemoryoptimizedcores",
                },
            }

            for usage in usages:
                if usage.name and usage.name.value:
                    api_name = usage.name.value.lower()
                    # Accept match if it's in the explicit map *or*
                    # (requested unit is "cores" and the API string clearly
                    #  references cores / gpus – these represent compute quotas).
                    if (
                        api_name in unit_mappings.get(requested_unit, {requested_unit})
                        or (
                            requested_unit == "cores"
                            and ("core" in api_name or "gpu" in api_name)
                        )
                    ):
                        limit = usage.limit
                        current_value = usage.current_value

                        if limit is None or current_value is None:
                            print(f"Warning: Missing limit or currentValue for {usage.name.value} in {region} for ContainerApps.")
                            continue

                        try:
                            limit_val = float(limit)
                            current_val = float(current_value)
                        except ValueError:
                            print(f"Warning: Non-numeric limit/currentValue for {usage.name.value} in {region} for ContainerApps.")
                            continue
                        
                        quota_info = QuotaInfo(
                            unit=capacity["unit"],
                            current_usage=current_val,
                            limit=limit_val,
                            required=float(capacity["required"])
                        )
                        result.quotas[capacity["unit"]] = quota_info
                        found_quota = True
                        break
                else:
                    print(f"Warning: Malformed usage object encountered for ContainerApps in {region}")

            if not found_quota and requested_unit == "cores" and resource_type == "Microsoft.App/managedEnvironments":
                # If we're checking cores for Container Apps and didn't find anything,
                # use a default of 100 cores per environment as per Azure documentation
                print(
                    f"Warning: No core quota found for Container Apps in {region}. "
                    f"Using default 100 cores per environment limit."
                )
                quota_info = QuotaInfo(
                    unit=capacity["unit"],
                    current_usage=0,
                    limit=100.0,  # Default 100 cores per environment
                    required=float(capacity["required"])
                )
                result.quotas[capacity["unit"]] = quota_info
            elif not found_quota:
                available_units = [
                    u.name.value
                    for u in usages
                    if hasattr(u, "name") and u.name and hasattr(u.name, "value") and u.name.value
                ]
                print(
                    f"Warning: Quota unit '{capacity['unit']}' not found for "
                    f"{resource_type} in {region}. Available: {available_units}"
                )
                # If quota unit not found, mark as insufficient
                result.quotas[capacity["unit"]] = QuotaInfo(
                    unit=capacity["unit"],
                    current_usage=0,
                    limit=0,
                    required=float(capacity["required"])
                )

        return result

class QuotaClientAdapter(ProviderAdapter):
    """Fallback adapter using Microsoft.Quota."""
    
    def check_quota(self, resource_type: str, region: str, capacity: Dict) -> ResourceQuota:
        from azure.mgmt.quota import QuotaManagementClient
        
        client = QuotaManagementClient(self.credential, self.subscription_id)
        quotas = client.quotas.list(resource_type, region)
        
        result = ResourceQuota(resource_type, region, {})
        
        for quota in quotas:
            if quota.properties.limit_name.lower() == capacity["unit"].lower():
                quota_info = QuotaInfo(
                    unit=capacity["unit"],
                    current_usage=quota.properties.current_value,
                    limit=quota.properties.limit_value,
                    required=capacity["required"]
                )
                result.quotas[capacity["unit"]] = quota_info
                break
        
        return result

class ProviderAdapterRegistry:
    """Registry of provider adapters with fallback logic."""
    
    def __init__(self, subscription_id: str):
        """Initialize the registry.
        
        Args:
            subscription_id: Azure subscription ID.
        """
        self.subscription_id = subscription_id
        self.adapters = {
            "Microsoft.Compute": ComputeProviderAdapter(subscription_id),
            "Microsoft.Web": WebProviderAdapter(subscription_id),
            "Microsoft.DBforPostgreSQL": PostgreSQLProviderAdapter(subscription_id),
            "Microsoft.App": ContainerAppsProviderAdapter(subscription_id),
            # Register other provider adapters
        }
        self.fallback = QuotaClientAdapter(subscription_id)
    
    def get_adapter(self, resource_type: str) -> ProviderAdapter:
        """Get the appropriate adapter for a resource type, with fallback.
        
        Args:
            resource_type: Azure resource type (e.g., "Microsoft.Web/staticSites").
            
        Returns:
            ProviderAdapter: The appropriate adapter for the resource type.
        """
        provider = resource_type.split('/')[0]
        return self.adapters.get(provider, self.fallback)
