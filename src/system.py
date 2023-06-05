import numpy as np
import src.state_generator as gen
from src.opt import mpc_solver
import hdbscan
import scipy.cluster.hierarchy as hcluster
import time



class LinearAgentNd():
    """Linear agent with x[t+1] = Ax[t] + Bu[t] dynamics."""

    def __init__(self,
                 A, B,
                 agent_dim=1,
                 init_state=np.zeros((1))) -> None:
        """
        Args:
            A, B:           State and control transition matrices
            agent_dim:      Agent dimensionality
            init_state:     Initial agent state   
        """
        assert init_state.size == agent_dim
        self.A = A
        self.B = B
        assert A.shape == (agent_dim, agent_dim)
        assert B.shape[1] == agent_dim
        self.state = init_state
        self.agent_dim = agent_dim
        
    def propagate_input(self, control_val):
        """Receive a control action and change agent state correspondingly."""
        self.state = self.A @ self.state + self.B @ control_val
    
    # !!! Prefer not to use
    def set_state(self, input_state):
        """Manually set specific agent state; avoid using it and prefer propagate_input()."""
        assert self.state.shape == input_state.shape
        self.state = input_state


class LinearClusterNd():
    """Linear cluster of agents with A = avg(A_i) and B = avg(B_i) for all agents i in the cluster."""

    def __init__(self,
                 agents,
                 n_agents,
                 agent_dim) -> None:
        """
        Args:
            agents:         Dictionary of agents combined into the cluster
            n_agents:       Number of agents in the cluster
            agent_dim:      Agent dimensionality
        """
        self.agents = agents
        assert n_agents == len(agents)
        self.n_agents = n_agents
        self.full_cluster_state = np.zeros((n_agents, agent_dim))
        self.centroid = self.update_centroid()

    def propagate_input(self, control_val):
        """Propagate a meso-scale control action for all agents within the cluster."""
        for agent in self.agents.values():
            agent.propagate_input(control_val)
        self.update_centroid()

    def update_centroid(self):
        """Update the centroid value according to the aggregated agent state."""
        for idx in range(self.n_agents):
            self.full_cluster_state[idx] = list(self.agents.values())[idx].state
        self.centroid = np.mean(self.full_cluster_state, axis=0)
        return self.centroid


class MultiAgentSystem():
    """A multi-agent system dynamics simulator."""

    def __init__(self, n_agents=1, agent_dim=1, control_dim=1, global_goal=np.array([0]),
                 state_gen=gen.random_blobs, state_gen_args=[1, 1, 1, 1, 10],
                 clust_algo='hierarchy', clust_algo_params=[2]) -> None:
        """
        Args:
            n_agents:               Number of agents
            agent_dim:              Agent dimensionality
            control_dim:            Control input dimensionality
            global_goal:            Goal point coordinates
            state_gen:              Initial agent state generator 
            state_gen_args:         Parameters of the initial state generator
            clust_algo:             Cluster identification algorithm
            clust_algo_params:      Parameters of cluster identification (depending on the algorithm):
                ________________________________________________
                algo        | parameters
                ________________________________________________
                hierarchy   | threshold
                hdbscan     | alpha, leaf size, min_cluster_size
        """
        self.n_agents = n_agents
        self.agent_dim = agent_dim
        self.control_dim = control_dim
        self.full_system_state = np.zeros((n_agents, agent_dim))
        self.system_goal = global_goal
        self.agents = state_gen(LinearAgentNd, agent_dim, n_agents, *state_gen_args)
        self.clust_algo = clust_algo
        self.clust_algo_params = clust_algo_params
        self.avg_goal_dist = np.inf
        self.control_solution_time = 0.
        self._re_eval_system()

    def _re_eval_system(self):
        """Re-evaluate full system state by gathering each agent states"""
        for idx, agent in self.agents.items():
            self.full_system_state[idx] = agent.state
        self.avg_goal_dist = np.linalg.norm(self.full_system_state - self.system_goal, axis=1).mean(axis=0)
        self._re_eval_clusters()

    def _re_eval_clusters(self):
        """Re-evaluate clusters"""
        algo = self.clust_algo
        algo_parameters = self.clust_algo_params
        if algo == 'hierarchy':
            thresh = algo_parameters[0]
            self.clust_labels = hcluster.fclusterdata(self.full_system_state, thresh, criterion='distance') - 1
        elif algo == 'hdbscan':
            alpha, leaf_size, min_cluster_size = algo_parameters
            clusterer = hdbscan.HDBSCAN(alpha=alpha, 
                                        leaf_size=leaf_size, 
                                        min_cluster_size=min_cluster_size,
                                        min_samples=1)
            clusterer.fit(self.full_system_state)
            self.clust_labels = clusterer.labels_
        # TODO epsdel
        #elif algo == 'epsdel':
        #    epsv, delv = clust_parameters
        #    assert delv <= epsv
        else:
            raise ValueError("Cluster identification algorithm not implemented")
        
        self.n_clusters = max(self.clust_labels) + 1
        self.clusters = {}
        self.cluster_centroids = {}
        for cdx in range(self.n_clusters):
            agent_indices = np.where(self.clust_labels == cdx)[0]
            n_agents_clust = agent_indices.size
            cluster = LinearClusterNd({loc_idx : self.agents[loc_idx] for loc_idx in agent_indices},
                                      n_agents_clust,
                                      self.agent_dim)
            self.clusters[cdx] = cluster 
            self.cluster_centroids[cdx] = cluster.centroid
    
    # Non-correct simplified implementation
    def update_system_simplified(self, step_size=0.01):
        """
        Simple descent algorithm: cluster states are corrected 
        according to the fraction of the distance toward the goal.
        
        Args:
            step_size:      Gradient step size
        """
        for cluster in self.clusters:
            centroid = cluster.centroid
            meso_control = step_size * (self.system_goal - centroid)
            cluster.propagate_input(meso_control)
            self._re_eval_system()

    def update_system_mpc_distributed(self, Q, R, P, n_t=10, umax=None, umin=None):
        """
        'Distributed' (iterated) MPC algorithm: agent states are corrected
        according to a micro-scale controller derived by optimizing MPC cost
        for each agent iteratively and separately.

        Args:
            Q:              State-cost weight matrix
            R:              Control-cost weight matrix
            P:              Terminal-state-cost weight matrix
            n_t:            Number of time steps in MPC
            umax, umin:     Control value constraints
        
        Returns:
            avg_goal_dist:      Average distance toward the goal point for all agents
            cost_val:           Value of the cost function at the final step        
        """
        time_0 = time.time()
        cost_val = 0.
        for _, agent in self.agents.items():
            A = agent.A
            B = agent.B
            x0 = agent.state
            state_dynamics, u_dynamics, cost_val_agnt = mpc_solver.use_modeling_tool(A, B, n_t, 
                                                                                     Q, R, P, x0, 
                                                                                     x_star_in=self.system_goal,
                                                                                     umax=umax, umin=umin)
            cost_val += cost_val_agnt
            agent.set_state(state_dynamics[:, -1])
            self._re_eval_system()
        self.control_solution_time += time.time() - time_0
        cost_val /= self.n_agents
        return self.avg_goal_dist, cost_val

    def update_system_mpc(self, Q, R, P, n_t=10, umax=None, umin=None):
        """
        Full-state MPC algorithm: agent states are corrected
        according to a micro-scale controller derived by optimizing MPC cost
        for the full system state by combining each agent state into a 
        'n_agents * agent_dim'-dimensional vector.

        Args:
            Q:              State-cost weight matrix
            R:              Control-cost weight matrix
            P:              Terminal-state-cost weight matrix
            n_t:            Number of time steps in MPC
            umax, umin:     Control value constraints
        
        Returns:
            avg_goal_dist:      Average distance toward the goal point for all agents
            cost_val:           Value of the cost function at the final step        
        """
        time_0 = time.time()
        A = np.zeros((self.agent_dim * self.n_agents, self.agent_dim * self.n_agents))
        B = np.zeros((self.agent_dim * self.n_agents, self.control_dim * self.n_agents))
        for adx, agent in self.agents.items():
            A[adx * self.agent_dim : (adx + 1) * self.agent_dim,
              adx * self.agent_dim : (adx + 1) * self.agent_dim] = agent.A
            B[adx * self.agent_dim : (adx + 1) * self.agent_dim,
              adx * self.control_dim: (adx + 1) * self.control_dim] = agent.B
        x0 = self.full_system_state.flatten()
        goal = np.kron(np.ones((self.n_agents)), self.system_goal)
        state_dynamics, u_dynamics, cost_val = mpc_solver.use_modeling_tool(A, B, n_t, 
                                                                            Q, R, P, x0, 
                                                                            x_star_in=goal,
                                                                            umax=umax, umin=umin)
        self.control_solution_time += time.time() - time_0
        for adx, agent in self.agents.items():
            agent.set_state(state_dynamics[adx * self.agent_dim : (adx + 1) * self.agent_dim, -1])
        self._re_eval_system()
        return self.avg_goal_dist, cost_val
        

