"""Manages the creation and deployment of desired services configuration."""
# coding=utf-8
#
# Copyright 2017 F5 Networks Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from __future__ import print_function

import logging
from time import time

import f5_cccl.exceptions as exc
from f5_cccl.service.config_reader import ServiceConfigReader
from f5_cccl.service.validation import ServiceConfigValidator
from f5_cccl.resource.ltm.node import ApiNode
from f5_cccl.utils.route_domain import (
    encoded_normalize_address_with_route_domain)
from f5_cccl.utils.route_domain import split_ip_with_route_domain


LOGGER = logging.getLogger(__name__)


# Check for upgrade issues on first pass only


class ServiceConfigDeployer(object):
    """CCCL config deployer class."""

    first_pass = True

    def __init__(self, bigip_proxy):
        """Initialize the config deployer."""
        self._bigip = bigip_proxy

    def _get_resource_tasks(self, existing, desired):
        """Get the list of resources to create, delete, update."""
        create_list = [
            desired[resource] for resource in
            set(desired) - set(existing)
        ]
        update_list = set(desired) & set(existing)
        update_list = [
            desired[resource] for resource in update_list
            if desired[resource] != existing[resource]
        ]
        delete_list = [
            existing[resource] for resource in
            set(existing) - set(desired)
        ]
        return (create_list, update_list, delete_list)

    def _create_resources(self, create_list):
        """Iterate over the resources and call create method."""
        LOGGER.debug("Creating %d resources...", len(create_list))
        retry_list = list()
        for resource in create_list:
            try:
                start_time = time()
                resource.create(self._bigip.mgmt_root())
                LOGGER.debug("Created %s in %.5f seconds.",
                             resource.name, (time() - start_time))
            except exc.F5CcclResourceConflictError:
                LOGGER.warning(
                    "Resource /%s/%s already exists, skipping task...",
                    resource.partition, resource.name)
            except (exc.F5CcclResourceCreateError,
                    exc.F5CcclError) as e:
                LOGGER.error(str(e))
                LOGGER.error(
                    "Resource /%s/%s creation error, requeuing task...",
                    resource.partition, resource.name)
                retry_list.append(resource)

        return retry_list

    def _update_resources(self, update_list):
        """Iterate over the resources and call update method."""
        LOGGER.debug("Updating %d resources...", len(update_list))
        retry_list = list()
        for resource in update_list:
            try:
                start_time = time()
                resource.update(self._bigip.mgmt_root())
                LOGGER.debug("Updated %s in %.5f seconds.",
                             resource.name, (time() - start_time))
            except exc.F5CcclResourceNotFoundError as e:
                LOGGER.warning(
                    "Resource /%s/%s does not exist, skipping task...",
                    resource.partition, resource.name)
            except (exc.F5CcclResourceUpdateError,
                    exc.F5CcclResourceRequestError,
                    exc.F5CcclError) as e:
                LOGGER.error(str(e))
                LOGGER.error(
                    "Resource /%s/%s update error, requeuing task...",
                    resource.partition, resource.name)
                retry_list.append(resource)

        return retry_list

    def _delete_resources(self, delete_list, retry=True):
        """Iterate over the resources and call delete method."""
        LOGGER.debug("Deleting %d resources...", len(delete_list))
        retry_list = list()
        for resource in delete_list:
            try:
                start_time = time()
                resource.delete(self._bigip.mgmt_root())
                LOGGER.debug("Deleted %s in %.5f seconds.",
                             resource.name, (time() - start_time))
            except exc.F5CcclResourceNotFoundError:
                LOGGER.warning(
                    "Resource /%s/%s does not exist, skipping task...",
                    resource.partition, resource.name)
            except (exc.F5CcclResourceDeleteError,
                    exc.F5CcclResourceRequestError,
                    exc.F5CcclError) as e:
                LOGGER.error(str(e))
                if retry:
                    LOGGER.error(
                        "Resource /%s/%s delete error, requeuing task...",
                        resource.partition, resource.name)
                    retry_list.append(resource)

        return retry_list

    def _get_monitor_tasks(self, desired_config):
        """Get CRUD tasks for all monitors."""
        create_monitors = list()
        delete_monitors = list()
        update_monitors = list()

        for hm_type in ['http', 'https', 'tcp', 'icmp', 'udp']:
            existing = self._bigip.get_monitors(hm_type)
            config_key = "{}_monitors".format(hm_type)
            desired = desired_config.get(config_key, dict())

            (create_hm, update_hm, delete_hm) = (
                self._get_resource_tasks(existing, desired))

            create_monitors += create_hm
            update_monitors += update_hm
            delete_monitors += delete_hm

        return (create_monitors, update_monitors, delete_monitors)

    def _get_user_tunnel_tasks(self, desired):
        """Get the update tasks for user-created fdb tunnels."""
        all_tunnels = self._bigip.get_fdb_tunnels(all_tunnels=True)
        # Get only the tunnels we desire
        update_list = set(desired) & set(all_tunnels)
        update_list = [
            desired[resource] for resource in update_list
            if desired[resource] != all_tunnels[resource]
        ]

        return update_list

    def _desired_nodes(self, default_route_domain):
        """Desired nodes is inferred from the active pool members."""
        desired_nodes = dict()

        nodes = self._bigip.get_nodes()
        pools = self._bigip.get_pools(True)
        for pool in pools:
            for member in pools[pool].members:
                pool_addr = member.name.split('%3A')[0]
                pool_addr_rd = encoded_normalize_address_with_route_domain(
                    pool_addr, default_route_domain, True, False)
                # make a copy to iterate over, then delete from 'nodes'
                node_list = list(nodes.keys())
                for key in node_list:
                    node_addr = nodes[key].data['address']
                    node_addr_rd = encoded_normalize_address_with_route_domain(
                        node_addr, default_route_domain, False, False)
                    if node_addr_rd == pool_addr_rd:
                        node = {'name': key,
                                'partition': nodes[key].partition,
                                'address': node_addr_rd,
                                'default_route_domain': default_route_domain,
                                'state': 'user-up',
                                'session': 'user-enabled'}
                        desired_node = ApiNode(**node)
                        desired_nodes[desired_node.name] = desired_node

        return desired_nodes

    # pylint: disable=too-many-locals
    def _pre_deploy_legacy_ltm_cleanup(self):
        """Remove legacy named resources (pre Route Domain support)

           We now create node resources with  names that include the route
           domain whether the end user specified them or not.  This prevents
           inconsistent behavior when the default route domain is changed for
           the managed partition.

           This function can be removed when the cccl version >= 2.0
        """

        # Detect legacy names (nodes do not include the route domain)
        self._bigip.refresh_ltm()
        existing_nodes = self._bigip.get_nodes()
        node_list = list(existing_nodes.keys())
        for node_name in node_list:
            route_domain = split_ip_with_route_domain(node_name)[1]
            if route_domain is None:
                break
        else:
            return

        existing_iapps = self._bigip.get_app_svcs()
        existing_virtuals = self._bigip.get_virtuals()
        existing_policies = self._bigip.get_l7policies()
        existing_irules = self._bigip.get_irules()
        existing_internal_data_groups = self._bigip.get_internal_data_groups()
        existing_pools = self._bigip.get_pools()

        delete_iapps = self._get_resource_tasks(existing_iapps, {})[-1]
        delete_virtuals = self._get_resource_tasks(existing_virtuals, {})[-1]
        delete_policies = self._get_resource_tasks(existing_policies, {})[-1]
        delete_irules = self._get_resource_tasks(existing_irules, {})[-1]
        delete_internal_data_groups = self._get_resource_tasks(
            existing_internal_data_groups, {})[-1]
        delete_pools = self._get_resource_tasks(existing_pools, {})[-1]
        delete_monitors = self._get_monitor_tasks({})[-1]

        delete_nodes = self._get_resource_tasks(existing_nodes, {})[-1]

        delete_tasks = delete_iapps + delete_virtuals + delete_policies + \
            delete_irules + delete_internal_data_groups + delete_pools + \
            delete_monitors + delete_nodes
        taskq_len = len(delete_tasks)

        finished = False
        LOGGER.debug("Removing legacy resources...")
        while not finished:
            LOGGER.debug("Legacy cleanup service task queue length: %d",
                         taskq_len)

            # Must remove all resources that depend on nodes (vs, pools, ???)
            delete_tasks = self._delete_resources(delete_tasks)

            tasks_remaining = len(delete_tasks)

            # Did the task queue shrink?
            if tasks_remaining >= taskq_len or tasks_remaining == 0:
                # No, we have stopped making progress.
                finished = True

            # Reset the taskq length.
            taskq_len = tasks_remaining

    def _post_deploy(self, desired_config, default_route_domain):
        """Perform post-deployment service tasks/cleanup.

        Remove superfluous resources that could not be inferred from the
        desired config.
        """
        LOGGER.debug("Perform post-deploy service tasks...")
        self._bigip.refresh_ltm()

        # Delete/update nodes (no creation)
        LOGGER.debug("Post-process nodes.")
        existing = self._bigip.get_nodes()
        desired = self._desired_nodes(default_route_domain)
        (update_nodes, delete_nodes) = \
            self._get_resource_tasks(existing, desired)[1:3]
        self._update_resources(update_nodes)
        self._delete_resources(delete_nodes)

        # Delete extraneous virtual addresses
        LOGGER.debug("Remove superfluous virtual addresses.")
        desired = desired_config.get('virtual_addresses', dict())
        (referenced, unreferenced) = (
            self._bigip.get_virtual_address_references()
        )
        delete_vaddrs = self._get_resource_tasks(unreferenced, desired)[2]
        self._delete_resources(delete_vaddrs)

        # Get the set of virtual addresses that are created by virtuals
        # but not in the set of desired virtual addresses.
        update_vaddrs = list()
        auto_created = self._get_resource_tasks(referenced, desired)[2]
        for vaddr in auto_created:
            if vaddr.data['enabled'] == "no":
                vaddr.data['enabled'] = "yes"
                update_vaddrs.append(vaddr)

        self._update_resources(update_vaddrs)

    def deploy_ltm(  # pylint: disable=too-many-locals,too-many-statements
            self, desired_config, default_route_domain):
        """Deploy the managed partition with the desired LTM config.

        :param desired_config: A dictionary with the configuration
        to be applied to the bigip managed partition.

        :returns: The number of tasks that could not be completed.
        """

        # Remove legacy resources (pre RD-named resources) before deploying
        # new configuration
        if ServiceConfigDeployer.first_pass:
            ServiceConfigDeployer.first_pass = False
            self._pre_deploy_legacy_ltm_cleanup()

        self._bigip.refresh_ltm()

        # Get the list of virtual address tasks
        LOGGER.debug("Getting virtual address tasks...")
        existing = self._bigip.get_virtual_addresses()
        desired = desired_config.get('virtual_addresses', dict())
        (create_vaddrs, update_vaddrs) = (
            self._get_resource_tasks(existing, desired))[0:2]

        # Get the list of virtual server tasks
        LOGGER.debug("Getting virtual server tasks...")
        existing_virtuals = self._bigip.get_virtuals()
        desired = desired_config.get('virtuals', dict())
        (create_virtuals, update_virtuals, delete_virtuals) = (
            self._get_resource_tasks(existing_virtuals, desired))

        # Get the list of pool tasks
        LOGGER.debug("Getting pool tasks...")
        existing_pools = self._bigip.get_pools()
        desired = desired_config.get('pools', dict())
        (create_pools, update_pools, delete_pools) = (
            self._get_resource_tasks(existing_pools, desired))

        # Get the list of irule tasks
        LOGGER.debug("Getting iRule tasks...")
        existing = self._bigip.get_irules()
        desired = desired_config.get('irules', dict())
        (create_irules, update_irules, delete_irules) = (
            self._get_resource_tasks(existing, desired))

        # Get the list of internal data group tasks
        LOGGER.debug("Getting InternalDataGroup tasks...")
        existing = self._bigip.get_internal_data_groups()
        desired = desired_config.get('internaldatagroups', dict())
        (create_internal_data_groups, update_internal_data_groups,
         delete_internal_data_groups) = (
             self._get_resource_tasks(existing, desired))

        # Get the list of policy tasks
        LOGGER.debug("Getting policy tasks...")
        existing = self._bigip.get_l7policies()
        desired = desired_config.get('l7policies', dict())
        (create_policies, update_policies, delete_policies) = (
            self._get_resource_tasks(existing, desired))

        # Get the list of iapp tasks
        LOGGER.debug("Getting iApp tasks...")
        existing_iapps = self._bigip.get_app_svcs()
        desired = desired_config.get('iapps', dict())
        (create_iapps, update_iapps, delete_iapps) = (
            self._get_resource_tasks(existing_iapps, desired))

        # Get the list of monitor tasks
        LOGGER.debug("Getting monitor tasks...")
        (create_monitors, update_monitors, delete_monitors) = (
            self._get_monitor_tasks(desired_config))

        LOGGER.debug("Building task lists...")
        create_tasks = create_vaddrs + create_monitors + \
            create_pools + create_internal_data_groups + create_irules + \
            create_policies + create_virtuals + create_iapps
        update_tasks = update_vaddrs + update_monitors + \
            update_pools + update_internal_data_groups + update_irules + \
            update_policies + update_virtuals + update_iapps
        delete_tasks = delete_iapps + delete_virtuals + delete_policies + \
            delete_irules + delete_internal_data_groups + delete_pools + \
            delete_monitors

        taskq_len = len(create_tasks) + len(update_tasks) + len(delete_tasks)

        taskq_len = self._run_tasks(
            taskq_len, create_tasks, update_tasks, delete_tasks)

        self._post_deploy(desired_config, default_route_domain)

        return taskq_len

    def deploy_net(self, desired_config):  # pylint: disable=too-many-locals
        """Deploy the managed partition with the desired NET config.

        :param desired_config: A dictionary with the configuration
        to be applied to the bigip managed partition.

        :returns: The number of tasks that could not be completed.
        """
        self._bigip.refresh_net()

        # Get the list of arp tasks
        LOGGER.debug("Getting arp tasks...")
        existing = self._bigip.get_arps()
        desired = desired_config.get('arps', dict())
        (create_arps, update_arps, delete_arps) = (
            self._get_resource_tasks(existing, desired))

        # Get the list of tunnel tasks
        LOGGER.debug("Getting tunnel tasks...")
        existing = self._bigip.get_fdb_tunnels()
        desired = desired_config.get('fdbTunnels', dict())
        (create_tunnels, update_tunnels, delete_tunnels) = (
            self._get_resource_tasks(existing, desired))

        # If there are pre-existing (user-created) tunnels that we are
        # managing, we want to only update these tunnels.
        LOGGER.debug("Getting pre-existing tunnel update tasks...")
        desired = desired_config.get('userFdbTunnels', dict())
        update_existing_tunnels = self._get_user_tunnel_tasks(desired)

        LOGGER.debug("Building task lists...")
        create_tasks = create_arps + create_tunnels
        update_tasks = update_arps + update_tunnels + update_existing_tunnels
        delete_tasks = delete_arps + delete_tunnels

        taskq_len = len(create_tasks) + len(update_tasks) + len(delete_tasks)

        return self._run_tasks(
            taskq_len, create_tasks, update_tasks, delete_tasks)

    def _run_tasks(self, taskq_len, create_tasks, update_tasks, delete_tasks):
        """Create, update, and delete the necessary resources."""
        # 'finished' indicates that the task queue is empty, or there is
        # no way to continue to make progress.  If there are errors in
        # deploying any resource, it is saved in the queue until another
        # pass can be made to deploy the configuration.  When we have
        # gone through the queue on a pass without shrinking the task
        # queue, it is determined that progress has stopped and the
        # loop is exited with work remaining.
        finished = False
        while not finished:
            LOGGER.debug("Service task queue length: %d", taskq_len)

            # Iterate over the list of resources to create
            create_tasks = self._create_resources(create_tasks)

            # Iterate over the list of resources to update
            update_tasks = self._update_resources(update_tasks)

            # Iterate over the list of resources to delete
            delete_tasks = self._delete_resources(delete_tasks)

            tasks_remaining = (
                len(create_tasks) + len(update_tasks) + len(delete_tasks))

            # Did the task queue shrink?
            if tasks_remaining >= taskq_len or tasks_remaining == 0:
                # No, we have stopped making progress.
                finished = True

            # Reset the taskq length.
            taskq_len = tasks_remaining

        return taskq_len


class ServiceManager(object):
    """CCCL apply config implementation class."""

    def __init__(self, bigip_proxy, partition, schema):
        """Initialize the ServiceManager.

        Args:
            bigip_proxy:  BigIPProxy object, f5_cccl.bigip.BigIPProxy.
            partition: The managed partition.
            schema: Schema that defines the structure of a service
            configuration.

        Raises:
            F5CcclError: Error initializing the validator or reading the
            API schema.
        """
        self._partition = partition
        self._bigip = bigip_proxy
        self._config_validator = ServiceConfigValidator(schema)
        self._service_deployer = ServiceConfigDeployer(bigip_proxy)
        self._config_reader = ServiceConfigReader(self._partition)

    def get_partition(self):
        """Get the name of the managed partition."""
        return self._partition

    def apply_ltm_config(self, service_config):
        """Apply the desired LTM service configuration.
        Args:
            service_config: The desired configuration state of the managed
            partition.

        Returns:
            The number of resources that were not successfully deployed.

        Raises:
            F5CcclValidationError: Indicates that the service_configuration
            does not conform to the API schema.
        """

        LOGGER.debug("apply_ltm_config start")
        start_time = time()

        # Validate the service configuration.
        self._config_validator.validate(service_config)

        # Determine the default route domain for the partition
        default_route_domain = self._bigip.get_default_route_domain()

        # Read in the configuration
        desired_config = self._config_reader.read_ltm_config(
            service_config, default_route_domain)

        # Deploy the service desired configuration.
        retval = self._service_deployer.deploy_ltm(
            desired_config, default_route_domain)

        LOGGER.debug(
            "apply_ltm_config took %.5f seconds.", (time() - start_time))

        return retval

    def apply_net_config(self, service_config):
        """Apply the desired NET service configuration.
        Args:
            service_config: The desired configuration state of the managed
            partition.

        Returns:
            The number of resources that were not successfully deployed.

        Raises:
            F5CcclValidationError: Indicates that the service_configuration
            does not conform to the API schema.
        """

        LOGGER.debug("apply_net_config start")
        start_time = time()

        # Validate the service configuration.
        self._config_validator.validate(service_config)

        # Determine the default route domain for the partition
        default_route_domain = self._bigip.get_default_route_domain()

        # Read in the configuration
        desired_config = self._config_reader.read_net_config(
            service_config, default_route_domain)

        # Deploy the service desired configuration.
        retval = self._service_deployer.deploy_net(desired_config)

        LOGGER.debug(
            "apply_net_config took %.5f seconds.", (time() - start_time))

        return retval
